from __future__ import annotations

import asyncio
import json
import smtplib
from email.message import EmailMessage
from pathlib import Path

import httpx

from app.config import Settings, get_settings
from app.models import Cart, Company, Order
from app.services.security import sign_action_token, webhook_signature


def order_payload(company: Company, order: Order, cart: Cart, settings: Settings) -> dict:
    review_token = sign_action_token(
        {"order_id": order.id, "company_id": company.id, "action": "review"},
        settings.order_action_secret,
    )
    return {
        "event": "order.company_approval_requested",
        "company": {"id": company.id, "name": company.name, "slug": company.slug},
        "order": {
            "id": order.id,
            "status": order.status,
            "estimated_total": str(order.estimated_total)
            if order.estimated_total is not None
            else None,
            "currency": order.currency,
            "customer": {
                "name": order.customer_name,
                "email": order.customer_email,
                "phone": order.customer_phone,
                "company": order.customer_company,
                "delivery_address": order.delivery_address,
                "note": order.customer_note,
            },
            "items": [
                {
                    "sku": item.sku,
                    "name": item.name,
                    "quantity": str(item.quantity),
                    "unit": item.unit,
                    "unit_price": str(item.unit_price) if item.unit_price is not None else None,
                    "currency": item.currency,
                    "notes": item.notes,
                    "evidence": json.loads(item.evidence_json or "[]"),
                }
                for item in cart.items
            ],
        },
        "review_url": (
            f"{settings.public_base_url.rstrip('/')}/?company={company.slug}&admin=1"
            f"&order_id={order.id}&review_token={review_token}"
        ),
        "policy": (
            "This request is pending company approval. No payment, production, "
            "shipping, or fulfillment has been authorized."
        ),
    }


class NotificationService:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    async def _send_webhook(self, url: str, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        secret = self.settings.order_webhook_secret
        if secret:
            headers["X-OMDI-Signature"] = webhook_signature(body, secret)
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, content=body, headers=headers)
            response.raise_for_status()

    def _send_email_sync(self, recipient: str, payload: dict) -> None:
        if not self.settings.smtp_host:
            raise RuntimeError("SMTP is not configured.")
        message = EmailMessage()
        message["Subject"] = f"Order approval required: {payload['order']['id']}"
        message["From"] = self.settings.smtp_from or self.settings.smtp_username
        message["To"] = recipient
        message.set_content(
            "A customer order/quote request needs company approval.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )
        with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=20) as smtp:
            if self.settings.smtp_use_tls:
                smtp.starttls()
            if self.settings.smtp_username:
                smtp.login(
                    self.settings.smtp_username,
                    self.settings.smtp_password or "",
                )
            smtp.send_message(message)

    async def notify_company(self, company: Company, order: Order, cart: Cart) -> str:
        payload = order_payload(company, order, cart, self.settings)
        webhook_url = company.sales_webhook_url or self.settings.order_webhook_url
        if webhook_url:
            await self._send_webhook(webhook_url, payload)
            return "webhook"
        if company.sales_email and self.settings.smtp_host:
            await asyncio.to_thread(self._send_email_sync, company.sales_email, payload)
            return "email"

        outbox_path = Path(self.settings.data_dir) / "outbox" / f"{order.id}.json"
        outbox_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return "outbox"
