from __future__ import annotations

import json
import re

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, Conversation, Message
from app.schemas import ChatResponse, Citation, Recommendation
from app.services.llm import ChatProvider, get_chat_provider, parse_json_response
from app.services.retrieval import (
    RetrievalService,
    RetrievedChunk,
    fold_for_matching,
    is_catalog_overview_query,
)

BASE_SYSTEM_PROMPT = """
You are a trustworthy consultative sales assistant for {company_name}.
Reply in the customer's language. Sound like a clear, friendly salesperson, not a database.

Answer style:
- Answer the customer's question immediately.
- Default to 2-5 short sentences or a short list, normally under 100 words.
- Use everyday language. Do not dump catalog text, internal metadata, or technical caveats.
- For a broad catalog question, list product families only. Do not present a random sample
  of individual SKUs as the complete catalog.
- Ask no more than one simple follow-up question, and only when it helps the sale.
- Do not repeat the answer in recommendations or in the follow-up question.

Grounding rules:
- Use only the supplied source excerpts for product facts, prices, dimensions, stock,
  minimum quantities, shipping, returns, warranties, and company policies.
- Cite factual claims with source IDs such as [S1]. Never invent a citation.
- A catalog listing proves that a product is offered; it does not prove current physical
  stock. Never say "in stock" or "available now" without explicit inventory evidence.
- When a source lists a color, say it is a listed color option. Do not describe that
  color as currently in stock.
- Preserve commercial qualifiers such as VAT/KDV, per-metre vs per-piece pricing,
  colors, pack quantities, bulk thresholds, and effective dates.
- Discuss a source conflict only when it affects the customer's current question.
- Never claim that a price, stock level, delivery date, discount, or custom specification
  is final unless an authoritative source explicitly says so.
- If the evidence is incomplete, say so briefly and ask the one detail needed to continue.

Sales behavior:
- Understand the use case before recommending.
- Recommend only products supported by evidence and explain why each is suitable.
- Return recommendation objects only for specific, relevant products. For product-family
  overviews or general company questions, return an empty recommendations list.
- Do not use pressure, fake scarcity, deceptive claims, or unsupported comparisons.
- When the buyer shows intent, offer to prepare a quote/order request.

Mandatory order policy:
- A submitted cart is only a REQUEST and must be sent to the company.
- The company must approve it before checkout, payment, production, shipping, or fulfillment.
- Never tell the customer that an unapproved request is confirmed.
- Do not collect card or bank credentials in chat.
- Do not ask for or collect delivery addresses, phone numbers, or email addresses in
  chat. Direct an interested buyer to the quote/order request form instead.
- Mention this approval policy only when discussing a quote, cart, order, or purchase request.

Return one JSON object with:
{{
  "answer": "answer with [S#] citations",
  "recommendations": [
    {{
      "sku": "optional",
      "name": "product name",
      "reason": "evidence-based reason",
      "suggested_quantity": 0,
      "unit": "metre/piece/etc",
      "unit_price": null,
      "currency": "TRY",
      "price_note": "VAT, tier, uncertainty, or source note",
      "evidence_ids": ["S1"]
    }}
  ],
  "next_question": "one question or null",
  "should_offer_quote": false
}}
""".strip()


def _history_text(messages: list[Message]) -> str:
    return "\n".join(f"{message.role}: {message.content}" for message in messages[-8:])


def _source_block(chunks: list[RetrievedChunk]) -> tuple[str, dict[str, RetrievedChunk]]:
    mapping: dict[str, RetrievedChunk] = {}
    blocks = []
    for index, chunk in enumerate(chunks, start=1):
        source_id = f"S{index}"
        mapping[source_id] = chunk
        location = []
        if chunk.page:
            location.append(f"page {chunk.page}")
        if chunk.section:
            location.append(chunk.section)
        blocks.append(
            f"### [{source_id}] {chunk.source_name}\n"
            f"Origin: {chunk.origin}\n"
            f"Authority: {chunk.authority_score}/100\n"
            f"Location: {', '.join(location) or 'not specified'}\n"
            f"{chunk.text}"
        )
    return "\n\n".join(blocks), mapping


def _valid_evidence_ids(values: list[str], source_map: dict[str, RetrievedChunk]) -> list[str]:
    return [value for value in values if value in source_map]


def _contextual_retrieval_query(message: str, history: list[Message]) -> str:
    if not history or re.search(r"\b[A-Za-z]\d+(?:\.\d+)?\b", message):
        return message
    folded = fold_for_matching(message)
    words = re.findall(r"\w+", folded)
    follow_up_markers = (
        "it",
        "that",
        "this",
        "them",
        "its",
        "how much",
        "what price",
        "ne kadar",
        "fiyati",
        "stokta",
        "hangi renk",
        "uzunlugu",
        "minimum",
    )
    if len(words) > 5 and not any(marker in folded for marker in follow_up_markers):
        return message
    recent = "\n".join(
        f"{item.role}: {item.content}" for item in history[-2:] if item.content.strip()
    )
    return f"{message}\nRecent product context:\n{recent}" if recent else message


def _company_profile_chunk(company: Company) -> RetrievedChunk | None:
    if not company.description:
        return None
    return RetrievedChunk(
        chunk_id=f"company-profile:{company.id}",
        text=company.description,
        source_name=f"{company.name} company profile",
        origin=f"company:{company.slug}",
        authority_score=90,
        page=None,
        section="Configured company profile",
        score=1.0,
    )


class ChatService:
    def __init__(
        self,
        retrieval: RetrievalService | None = None,
        provider: ChatProvider | None = None,
    ):
        self.retrieval = retrieval or RetrievalService()
        self.provider = provider or get_chat_provider()

    def _conversation(
        self,
        db: Session,
        *,
        company_id: str,
        customer_session: str,
        conversation_id: str | None,
    ) -> Conversation:
        conversation = db.get(Conversation, conversation_id) if conversation_id else None
        if conversation:
            if (
                conversation.company_id != company_id
                or conversation.customer_session != customer_session
            ):
                raise ValueError("Conversation does not belong to this customer session.")
            return conversation
        conversation = Conversation(
            company_id=company_id,
            customer_session=customer_session,
        )
        db.add(conversation)
        db.flush()
        return conversation

    def respond(
        self,
        db: Session,
        *,
        company: Company,
        message: str,
        customer_session: str,
        conversation_id: str | None,
        customer_context: dict,
    ) -> ChatResponse:
        conversation = self._conversation(
            db,
            company_id=company.id,
            customer_session=customer_session,
            conversation_id=conversation_id,
        )
        history = db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.asc())
        ).all()
        db.add(Message(conversation_id=conversation.id, role="user", content=message))
        retrieval_query = _contextual_retrieval_query(message, history)
        request_type = (
            "catalog_overview"
            if is_catalog_overview_query(message)
            else "sales_question"
        )
        chunks = self.retrieval.retrieve(
            db,
            company_id=company.id,
            query=retrieval_query,
            limit=6,
            catalog_overview=request_type == "catalog_overview",
        )
        if request_type == "catalog_overview":
            profile = _company_profile_chunk(company)
            if profile:
                chunks = [profile, *chunks[:5]]
        source_text, source_map = _source_block(chunks)
        system_prompt = BASE_SYSTEM_PROMPT.format(company_name=company.name)
        if company.description:
            system_prompt += f"\n\nCompany description:\n{company.description}"
        if company.prompt_overrides:
            system_prompt += f"\n\nCompany-specific instructions:\n{company.prompt_overrides}"
        user_prompt = (
            f"Conversation history:\n{_history_text(history) or '(new conversation)'}\n\n"
            f"Customer context:\n{json.dumps(customer_context, ensure_ascii=False)}\n\n"
            f"Request type:\n{request_type}\n\n"
            f"Source excerpts:\n{source_text or '(no matching sources)'}\n\n"
            f"Customer request:\n{message}"
        )
        raw = self.provider.complete(system_prompt, user_prompt)
        payload = parse_json_response(raw)
        answer = str(payload.get("answer") or "").strip()
        if not answer:
            answer = (
                "I could not produce a grounded answer from the available sources. "
                "Please rephrase the question or ask the company to review it."
            )
        answer = re.sub(
            r"\[(S\d+)\]",
            lambda match: match.group(0) if match.group(1) in source_map else "",
            answer,
        )
        cited_ids = set(re.findall(r"\[(S\d+)\]", answer))
        citations = []
        for source_id in sorted(
            cited_ids & source_map.keys(),
            key=lambda value: int(value[1:]),
        ):
            chunk = source_map[source_id]
            citations.append(
                Citation(
                    id=source_id,
                    source_name=chunk.source_name,
                    origin=chunk.origin,
                    page=chunk.page,
                    section=chunk.section,
                    excerpt=re.sub(r"\s+", " ", chunk.text).strip()[:360],
                    authority_score=chunk.authority_score,
                )
            )

        recommendations = []
        for item in payload.get("recommendations") or []:
            if not isinstance(item, dict):
                continue
            item["evidence_ids"] = _valid_evidence_ids(
                [str(value) for value in item.get("evidence_ids") or []],
                source_map,
            )
            if not item["evidence_ids"]:
                continue
            try:
                recommendations.append(Recommendation.model_validate(item))
            except ValidationError:
                continue

        response = ChatResponse(
            conversation_id=conversation.id,
            answer=answer,
            sources=citations,
            recommendations=recommendations,
            next_question=payload.get("next_question") or None,
            should_offer_quote=bool(payload.get("should_offer_quote", False)),
            commercial_confirmation_required=True,
        )
        db.add(
            Message(
                conversation_id=conversation.id,
                role="assistant",
                content=response.answer,
                metadata_json=response.model_dump_json(),
            )
        )
        db.commit()
        return response
