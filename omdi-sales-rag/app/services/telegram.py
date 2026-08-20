from __future__ import annotations

import asyncio
import difflib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from app.services.product_media import extract_skus

TELEGRAM_TEXT_LIMIT = 4096
COMPANY_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SalesAPIError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class TelegramStateStore:
    """Small durable mapping from a Telegram participant to a RAG conversation."""

    def __init__(self, path: Path):
        self.path = path
        self._chats: dict[str, dict[str, str | None]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        chats = payload.get("chats") if isinstance(payload, dict) else None
        if not isinstance(chats, dict):
            return
        for key, value in chats.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            company_slug = value.get("company_slug")
            conversation_id = value.get("conversation_id")
            if not isinstance(company_slug, str):
                continue
            self._chats[key] = {
                "company_slug": company_slug,
                "conversation_id": conversation_id if isinstance(conversation_id, str) else None,
            }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps({"chats": self._chats}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def get(self, participant_key: str, default_company_slug: str) -> dict[str, str | None]:
        state = self._chats.get(participant_key)
        if state is None:
            state = {
                "company_slug": default_company_slug,
                "conversation_id": None,
            }
            self._chats[participant_key] = state
            self._save()
        return dict(state)

    def update_conversation(
        self,
        participant_key: str,
        *,
        company_slug: str,
        conversation_id: str,
    ) -> None:
        self._chats[participant_key] = {
            "company_slug": company_slug,
            "conversation_id": conversation_id,
        }
        self._save()

    def reset(self, participant_key: str, default_company_slug: str) -> None:
        state = self.get(participant_key, default_company_slug)
        state["conversation_id"] = None
        self._chats[participant_key] = state
        self._save()

    def set_company(self, participant_key: str, company_slug: str) -> None:
        self._chats[participant_key] = {
            "company_slug": company_slug,
            "conversation_id": None,
        }
        self._save()


def parse_command(text: str) -> tuple[str, str] | None:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    first, _, remainder = stripped.partition(" ")
    command = first[1:].split("@", 1)[0].casefold()
    return command, remainder.strip()


def split_telegram_text(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    remaining = text.strip()
    if not remaining:
        return []
    chunks: list[str] = []
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _is_repeated_question(answer: str, next_question: str) -> bool:
    normalized_answer = re.sub(r"\s+", " ", answer.casefold()).strip()
    normalized_question = re.sub(r"\s+", " ", next_question.casefold()).strip()
    if normalized_question in normalized_answer:
        return True
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", normalized_answer)
        if sentence.strip()
    ]
    return any(
        difflib.SequenceMatcher(None, sentence, normalized_question).ratio() >= 0.72
        for sentence in sentences[-2:]
    )


def _image_candidates(message: str, response: dict[str, Any]) -> list[str]:
    direct = extract_skus(message)
    if direct:
        return direct[:1]
    folded = message.casefold()
    image_requested = any(
        word in folded
        for word in ("image", "picture", "photo", "resim", "görsel", "fotoğraf", "foto")
    )
    if not image_requested:
        return []
    candidates = []
    for recommendation in response.get("recommendations") or []:
        if not isinstance(recommendation, dict):
            continue
        sku = recommendation.get("sku")
        if isinstance(sku, str):
            try:
                candidates.extend(extract_skus(sku))
            except ValueError:
                continue
    return list(dict.fromkeys(candidates))[:1]


def format_chat_response(
    response: dict[str, Any],
    *,
    company_slug: str,
    public_base_url: str,
) -> str:
    answer = str(response.get("answer") or "").strip()
    answer = re.sub(r"\s*\[S\d+\]", "", answer)
    answer = re.sub(r" +([.,;:!?])", r"\1", answer)
    sections = [answer]

    next_question = str(response.get("next_question") or "").strip()
    if next_question and not _is_repeated_question(answer, next_question):
        sections.append(next_question)

    if response.get("should_offer_quote"):
        base_url = public_base_url.rstrip("/")
        if "YOUR_" not in base_url.upper():
            order_url = f"{base_url}/?company={quote(company_slug)}"
            sections.append(
                "Submit a quote/order request for company approval:\n"
                f"{order_url}"
            )

    return "\n\n".join(section for section in sections if section)


class TelegramSalesBot:
    def __init__(
        self,
        *,
        token: str,
        app_base_url: str,
        public_base_url: str,
        default_company_slug: str,
        state_path: Path,
        poll_timeout_seconds: int = 30,
        telegram_client: httpx.AsyncClient | None = None,
        sales_api_client: httpx.AsyncClient | None = None,
    ):
        self.default_company_slug = default_company_slug
        self.public_base_url = public_base_url
        self.poll_timeout_seconds = max(1, min(poll_timeout_seconds, 50))
        self.state = TelegramStateStore(state_path)
        self.telegram = telegram_client or httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}",
            timeout=httpx.Timeout(self.poll_timeout_seconds + 15, connect=15),
        )
        self.sales_api = sales_api_client or httpx.AsyncClient(
            base_url=app_base_url.rstrip("/"),
            timeout=httpx.Timeout(150, connect=15),
        )

    async def close(self) -> None:
        await self.telegram.aclose()
        await self.sales_api.aclose()

    async def _telegram_call(self, method: str, payload: dict[str, Any]) -> Any:
        for attempt in range(2):
            response = await self.telegram.post(f"/{method}", json=payload)
            body = response.json()
            if body.get("ok"):
                return body.get("result")
            retry_after = (body.get("parameters") or {}).get("retry_after")
            if response.status_code == 429 and retry_after and attempt == 0:
                await asyncio.sleep(min(int(retry_after), 60))
                continue
            description = body.get("description") or f"Telegram error {response.status_code}"
            raise RuntimeError(description)
        raise RuntimeError("Telegram request failed after retry.")

    async def _sales_request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self.sales_api.request(method, path, json=payload)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.is_error:
            detail = body.get("detail") if isinstance(body, dict) else None
            raise SalesAPIError(response.status_code, str(detail or "Sales API request failed."))
        return body

    async def _send_text(self, chat_id: int, text: str) -> None:
        for chunk in split_telegram_text(text):
            await self._telegram_call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": chunk,
                    "link_preview_options": {"is_disabled": True},
                },
            )

    async def _send_photo(self, chat_id: int, sku: str, content: bytes) -> None:
        response = await self.telegram.post(
            "/sendPhoto",
            data={"chat_id": str(chat_id), "caption": sku},
            files={"photo": (f"{sku}.jpg", content, "image/jpeg")},
        )
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("Telegram returned an invalid photo response.") from exc
        if not body.get("ok"):
            description = body.get("description") or f"Telegram error {response.status_code}"
            raise RuntimeError(description)

    async def _product_image(self, company_slug: str, sku: str) -> bytes | None:
        response = await self.sales_api.get(
            f"/api/companies/{quote(company_slug)}/products/{quote(sku)}/image"
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        if not response.headers.get("content-type", "").startswith("image/"):
            return None
        return response.content

    async def _typing(self, chat_id: int) -> None:
        try:
            await self._telegram_call(
                "sendChatAction",
                {"chat_id": chat_id, "action": "typing"},
            )
        except (httpx.HTTPError, RuntimeError, ValueError):
            return

    @staticmethod
    def _participant_key(message: dict[str, Any]) -> str:
        chat_id = message["chat"]["id"]
        user_id = (message.get("from") or {}).get("id", chat_id)
        return f"{chat_id}:{user_id}"

    async def _company(self, slug: str) -> dict[str, Any]:
        return await self._sales_request("GET", f"/api/companies/{quote(slug)}")

    async def _welcome(self, chat_id: int, slug: str) -> None:
        company = await self._company(slug)
        assistant = company.get("assistant_name") or "Sales Assistant"
        await self._send_text(
            chat_id,
            f"Hello! I am {assistant} for {company['name']}.\n\n"
            "Ask me about products, specifications, prices, or suitable options. "
            "Any quote or order request must be approved by the company before it proceeds.\n\n"
            "Commands:\n"
            "/new — start a new conversation\n"
            "/company <slug> — switch company\n"
            "/web — open the web assistant\n"
            "/help — show help",
        )

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("chat"), dict):
            return
        chat_id = message["chat"].get("id")
        if not isinstance(chat_id, int):
            return
        participant_key = self._participant_key(message)
        state = self.state.get(participant_key, self.default_company_slug)
        company_slug = str(state["company_slug"] or self.default_company_slug)
        text = message.get("text")

        if not isinstance(text, str) or not text.strip():
            await self._send_text(chat_id, "Please send your question as a text message.")
            return

        command = parse_command(text)
        if command:
            name, argument = command
            if name in {"start", "new"}:
                self.state.reset(participant_key, self.default_company_slug)
                refreshed = self.state.get(participant_key, self.default_company_slug)
                await self._welcome(chat_id, str(refreshed["company_slug"]))
                return
            if name == "help":
                await self._send_text(
                    chat_id,
                    "Send a product or sales question as normal text.\n\n"
                    "/new — reset conversation history\n"
                    "/company <slug> — select a configured company\n"
                    "/web — open the cart and approval-request interface",
                )
                return
            if name == "web":
                await self._send_text(
                    chat_id,
                    f"{self.public_base_url.rstrip('/')}/?company={quote(company_slug)}",
                )
                return
            if name == "company":
                if not argument or not COMPANY_SLUG_PATTERN.fullmatch(argument):
                    await self._send_text(chat_id, "Usage: /company company-slug")
                    return
                try:
                    company = await self._company(argument)
                except SalesAPIError as exc:
                    if exc.status_code == 404:
                        await self._send_text(chat_id, f'Company "{argument}" was not found.')
                        return
                    raise
                self.state.set_company(participant_key, argument)
                await self._send_text(
                    chat_id,
                    f"Company changed to {company['name']}. A new conversation has started.",
                )
                return
            await self._send_text(chat_id, "Unknown command. Use /help to see available commands.")
            return

        await self._typing(chat_id)
        user = message.get("from") or {}
        payload = {
            "message": text.strip(),
            "customer_session": f"telegram:{participant_key}",
            "conversation_id": state.get("conversation_id"),
            "customer_context": {
                "channel": "telegram",
                "chat_type": message["chat"].get("type"),
                "language_code": user.get("language_code"),
            },
        }
        response = await self._sales_request(
            "POST",
            f"/api/companies/{quote(company_slug)}/chat",
            payload=payload,
        )
        conversation_id = response.get("conversation_id")
        if isinstance(conversation_id, str):
            self.state.update_conversation(
                participant_key,
                company_slug=company_slug,
                conversation_id=conversation_id,
            )
        await self._send_text(
            chat_id,
            format_chat_response(
                response,
                company_slug=company_slug,
                public_base_url=self.public_base_url,
            ),
        )
        for sku in _image_candidates(text, response):
            try:
                image = await self._product_image(company_slug, sku)
                if image:
                    await self._send_photo(chat_id, sku, image)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                print(f"Product image delivery failed for {sku}: {exc}", flush=True)

    async def _wait_for_sales_api(self) -> None:
        for attempt in range(30):
            try:
                await self._company(self.default_company_slug)
                return
            except (httpx.HTTPError, SalesAPIError) as exc:
                if isinstance(exc, SalesAPIError) and exc.status_code == 404:
                    raise RuntimeError(
                        f'Company "{self.default_company_slug}" is not configured.'
                    ) from exc
                if attempt == 29:
                    raise RuntimeError("Sales API did not become ready.") from exc
                await asyncio.sleep(2)

    async def run(self) -> None:
        await self._wait_for_sales_api()
        bot = await self._telegram_call("getMe", {})
        print(f"Telegram bot ready: @{bot.get('username', 'unknown')}", flush=True)
        offset: int | None = None
        backoff = 1
        try:
            while True:
                try:
                    payload: dict[str, Any] = {
                        "timeout": self.poll_timeout_seconds,
                        "allowed_updates": ["message"],
                    }
                    if offset is not None:
                        payload["offset"] = offset
                    updates = await self._telegram_call("getUpdates", payload)
                    for update in updates or []:
                        update_id = update.get("update_id")
                        try:
                            await self.handle_update(update)
                        except SalesAPIError as exc:
                            message = update.get("message") or {}
                            chat = message.get("chat") or {}
                            chat_id = chat.get("id")
                            print(
                                f"Sales API error {exc.status_code}: {exc.detail}",
                                flush=True,
                            )
                            if isinstance(chat_id, int):
                                await self._send_text(
                                    chat_id,
                                    "The sales assistant is temporarily unavailable. "
                                    "Please try again shortly.",
                                )
                        except Exception as exc:  # noqa: BLE001 - isolate malformed updates
                            print(f"Telegram update failed: {exc}", flush=True)
                        finally:
                            if isinstance(update_id, int):
                                offset = update_id + 1
                    backoff = 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep long polling alive
                    print(f"Telegram polling error: {exc}", flush=True)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
        finally:
            await self.close()
