from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import httpx

from app.services.telegram import (
    TelegramSalesBot,
    TelegramStateStore,
    _image_candidates,
    format_chat_response,
    parse_command,
    split_telegram_text,
)


class TelegramTests(unittest.TestCase):
    def test_parse_command_supports_bot_username(self) -> None:
        self.assertEqual(
            parse_command("/company@omdi_bot another-company"),
            ("company", "another-company"),
        )
        self.assertIsNone(parse_command("normal question"))

    def test_long_messages_are_split_within_telegram_limit(self) -> None:
        text = "\n\n".join(f"Paragraph {index} " + ("x" * 700) for index in range(12))
        chunks = split_telegram_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 4096 for chunk in chunks))
        self.assertIn("Paragraph 0", chunks[0])
        self.assertIn("Paragraph 11", chunks[-1])

    def test_response_hides_sources_and_recommendation_duplication(self) -> None:
        response = {
            "answer": "Use Y6131 [S1].",
            "sources": [
                {
                    "id": "S1",
                    "source_name": "LED price list",
                    "page": 6,
                    "section": "Y6131",
                }
            ],
            "recommendations": [
                {
                    "sku": "Y6131",
                    "name": "Trimless LED profile",
                    "reason": "Suitable option",
                    "unit_price": 90,
                }
            ],
            "next_question": "How many metres do you need?",
            "should_offer_quote": True,
        }
        formatted = format_chat_response(
            response,
            company_slug="yigit-aluminium",
            public_base_url="https://sales.example.com",
        )
        self.assertIn("Use Y6131.", formatted)
        self.assertNotIn("[S1]", formatted)
        self.assertNotIn("Source", formatted)
        self.assertNotIn("Suitable options", formatted)
        self.assertNotIn("page 6", formatted)
        self.assertIn("company approval", formatted)
        self.assertIn(
            "https://sales.example.com/?company=yigit-aluminium",
            formatted,
        )

    def test_similar_next_question_is_not_repeated(self) -> None:
        formatted = format_chat_response(
            {
                "answer": "Siparişinizin teslimat adresini paylaşabilir misiniz?",
                "next_question": "Teslimat adresinizi paylaşabilir misiniz?",
            },
            company_slug="yigit-aluminium",
            public_base_url="https://sales.example.com",
        )
        self.assertEqual(formatted.count("paylaşabilir misiniz"), 1)

    def test_exact_sku_requests_product_image(self) -> None:
        self.assertEqual(_image_candidates("y6336 30 metre", {}), ["Y6336"])

    def test_state_is_persisted_between_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "telegram-state.json"
            store = TelegramStateStore(path)
            store.update_conversation(
                "1:2",
                company_slug="yigit-aluminium",
                conversation_id="conversation-1",
            )
            restored = TelegramStateStore(path).get("1:2", "fallback")
            self.assertEqual(restored["company_slug"], "yigit-aluminium")
            self.assertEqual(restored["conversation_id"], "conversation-1")


class TelegramBotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_message_is_forwarded_and_conversation_is_saved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            telegram_client = httpx.AsyncClient(
                base_url="https://api.telegram.org",
                trust_env=False,
            )
            sales_api_client = httpx.AsyncClient(
                base_url="http://sales-api",
                trust_env=False,
            )
            bot = TelegramSalesBot(
                token="test-token",
                app_base_url="http://sales-api",
                public_base_url="https://sales.example.com",
                default_company_slug="yigit-aluminium",
                state_path=Path(directory) / "state.json",
                telegram_client=telegram_client,
                sales_api_client=sales_api_client,
            )
            telegram_calls = []
            sales_calls = []

            async def fake_telegram_call(method, payload):
                telegram_calls.append((method, payload))
                return True

            async def fake_sales_request(method, path, *, payload=None):
                sales_calls.append((method, path, payload))
                return {
                    "conversation_id": "conversation-1",
                    "answer": "Y6131 is available [S1].",
                    "sources": [
                        {
                            "id": "S1",
                            "source_name": "LED price list",
                            "page": 6,
                            "section": "Y6131",
                        }
                    ],
                    "recommendations": [],
                    "next_question": None,
                    "should_offer_quote": False,
                }

            sent_photos = []

            async def fake_product_image(company_slug, sku):
                self.assertEqual(company_slug, "yigit-aluminium")
                self.assertEqual(sku, "Y6131")
                return b"image-bytes"

            async def fake_send_photo(chat_id, sku, content):
                sent_photos.append((chat_id, sku, content))

            bot._telegram_call = fake_telegram_call
            bot._sales_request = fake_sales_request
            bot._product_image = fake_product_image
            bot._send_photo = fake_send_photo
            try:
                await bot.handle_update(
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 100, "type": "private"},
                            "from": {"id": 200, "language_code": "en"},
                            "text": "Tell me about Y6131",
                        },
                    }
                )
            finally:
                await bot.close()

            self.assertEqual(sales_calls[0][0], "POST")
            self.assertEqual(
                sales_calls[0][1],
                "/api/companies/yigit-aluminium/chat",
            )
            self.assertEqual(
                sales_calls[0][2]["customer_session"],
                "telegram:100:200",
            )
            send_messages = [
                payload for method, payload in telegram_calls if method == "sendMessage"
            ]
            self.assertEqual(len(send_messages), 1)
            self.assertIn("Y6131 is available.", send_messages[0]["text"])
            self.assertNotIn("Source", send_messages[0]["text"])
            self.assertEqual(sent_photos, [(100, "Y6131", b"image-bytes")])
            restored = TelegramStateStore(Path(directory) / "state.json").get(
                "100:200",
                "fallback",
            )
            self.assertEqual(restored["conversation_id"], "conversation-1")


if __name__ == "__main__":
    unittest.main()
