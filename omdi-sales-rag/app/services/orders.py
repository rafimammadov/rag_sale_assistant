from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Cart, Company, Order, OrderEvent
from app.schemas import CustomerConfirmation, OrderDecision, OrderSubmit
from app.services.order_states import (
    COMPANY_APPROVED,
    CUSTOMER_CONFIRMED,
    PENDING_COMPANY_APPROVAL,
    assert_transition,
)


def calculate_estimated_total(cart: Cart) -> Decimal | None:
    if not cart.items or any(item.unit_price is None for item in cart.items):
        return None
    return sum(
        (item.quantity * item.unit_price for item in cart.items if item.unit_price is not None),
        start=Decimal("0"),
    )


def add_event(
    db: Session,
    order: Order,
    event: str,
    actor: str,
    details: dict | None = None,
) -> None:
    db.add(
        OrderEvent(
            order_id=order.id,
            event=event,
            actor=actor,
            details_json=json.dumps(details or {}, ensure_ascii=False, default=str),
        )
    )


class OrderService:
    def submit(
        self,
        db: Session,
        *,
        company: Company,
        payload: OrderSubmit,
    ) -> tuple[Order, bool]:
        existing = db.scalar(
            select(Order).where(
                Order.company_id == company.id,
                Order.submission_key == payload.submission_key,
            )
        )
        if existing:
            return existing, True
        cart = db.get(Cart, payload.cart_id)
        if not cart or cart.company_id != company.id:
            raise ValueError("Cart was not found for this company.")
        if cart.status != "draft":
            raise ValueError("Only a draft cart can be submitted.")
        if not cart.items:
            raise ValueError("The cart is empty.")
        currencies = {item.currency for item in cart.items}
        if len(currencies) != 1:
            raise ValueError("All cart items must use the same currency.")

        order = Order(
            company_id=company.id,
            cart_id=cart.id,
            submission_key=payload.submission_key,
            status=PENDING_COMPANY_APPROVAL,
            customer_name=payload.customer_name,
            customer_email=payload.customer_email,
            customer_phone=payload.customer_phone,
            customer_company=payload.customer_company,
            delivery_address=payload.delivery_address,
            customer_note=payload.customer_note,
            estimated_total=calculate_estimated_total(cart),
            currency=cart.items[0].currency,
        )
        cart.status = "submitted"
        db.add(order)
        db.flush()
        add_event(
            db,
            order,
            "submitted_for_company_approval",
            "customer",
            {"consent_to_share_with_company": True},
        )
        db.commit()
        db.refresh(order)
        return order, False

    def decide(
        self,
        db: Session,
        *,
        order: Order,
        target_status: str,
        decision: OrderDecision,
    ) -> Order:
        assert_transition(order.status, target_status)
        order.status = target_status
        order.company_note = decision.note
        order.approved_by = decision.actor
        order.decided_at = datetime.now(timezone.utc)
        if target_status == COMPANY_APPROVED:
            order.approved_total = decision.approved_total
            if decision.currency:
                order.currency = decision.currency
        add_event(
            db,
            order,
            target_status,
            decision.actor,
            {
                "note": decision.note,
                "approved_total": decision.approved_total,
                "currency": decision.currency,
            },
        )
        db.commit()
        db.refresh(order)
        return order

    def customer_confirm(
        self,
        db: Session,
        *,
        order: Order,
        cart: Cart,
        confirmation: CustomerConfirmation,
    ) -> Order:
        if cart.customer_session != confirmation.customer_session:
            raise ValueError("Order does not belong to this customer session.")
        if not confirmation.accept_company_terms:
            raise ValueError("The approved company terms must be accepted.")
        assert_transition(order.status, CUSTOMER_CONFIRMED)
        order.status = CUSTOMER_CONFIRMED
        order.customer_confirmed_at = datetime.now(timezone.utc)
        add_event(db, order, CUSTOMER_CONFIRMED, "customer")
        db.commit()
        db.refresh(order)
        return order
