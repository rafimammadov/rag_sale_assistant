from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CompanyCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)
    assistant_name: str = Field(default="Sales Assistant", max_length=120)
    website: str | None = None
    sales_email: str | None = None
    sales_webhook_url: str | None = None
    currency: str = Field(default="TRY", min_length=3, max_length=8)
    description: str | None = None
    prompt_overrides: str | None = None


class CompanyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    slug: str
    assistant_name: str
    website: str | None
    currency: str
    description: str | None
    created_at: datetime


class SourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    name: str
    origin: str
    authority_score: int
    status: str
    error: str | None
    retrieved_at: datetime


class ScrapeRequest(BaseModel):
    url: str
    max_pages: int = Field(default=40, ge=1, le=300)
    max_depth: int = Field(default=3, ge=0, le=8)
    authority_score: int = Field(default=70, ge=0, le=100)


class ScrapeResult(BaseModel):
    discovered: int
    indexed: int
    skipped: int
    failed: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    customer_session: str = Field(min_length=4, max_length=120)
    conversation_id: str | None = None
    customer_context: dict[str, Any] = Field(default_factory=dict)


class Citation(BaseModel):
    id: str
    source_name: str
    origin: str
    page: int | None = None
    section: str | None = None
    excerpt: str
    authority_score: int


class Recommendation(BaseModel):
    sku: str | None = None
    name: str
    reason: str
    suggested_quantity: Decimal | None = None
    unit: str | None = None
    unit_price: Decimal | None = None
    currency: str | None = None
    price_note: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    conversation_id: str
    answer: str
    sources: list[Citation]
    recommendations: list[Recommendation] = Field(default_factory=list)
    next_question: str | None = None
    should_offer_quote: bool = False
    commercial_confirmation_required: bool = True


class CartCreate(BaseModel):
    customer_session: str = Field(min_length=4, max_length=120)


class CartItemCreate(BaseModel):
    sku: str | None = Field(default=None, max_length=120)
    name: str = Field(min_length=1, max_length=500)
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1, max_length=40)
    unit_price: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(default="TRY", min_length=3, max_length=8)
    notes: str | None = None
    evidence: list[dict[str, Any] | str] = Field(default_factory=list)


class CartItemRead(CartItemCreate):
    model_config = ConfigDict(from_attributes=True)

    id: str


class CartRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    company_id: str
    customer_session: str
    status: str
    items: list[CartItemRead]
    created_at: datetime
    updated_at: datetime


class OrderSubmit(BaseModel):
    cart_id: str
    submission_key: str = Field(min_length=8, max_length=120)
    customer_name: str = Field(min_length=2, max_length=200)
    customer_email: str | None = Field(default=None, max_length=320)
    customer_phone: str | None = Field(default=None, max_length=80)
    customer_company: str | None = Field(default=None, max_length=200)
    delivery_address: str | None = None
    customer_note: str | None = None
    consent_to_share_with_company: bool

    @model_validator(mode="after")
    def require_contact_and_consent(self) -> OrderSubmit:
        if not self.customer_email and not self.customer_phone:
            raise ValueError("Provide at least an email address or phone number.")
        if not self.consent_to_share_with_company:
            raise ValueError("Consent is required before sending the request to the company.")
        return self


class OrderDecision(BaseModel):
    actor: str = Field(min_length=2, max_length=200)
    note: str | None = None
    approved_total: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=8)


class CustomerConfirmation(BaseModel):
    customer_session: str = Field(min_length=4, max_length=120)
    accept_company_terms: bool


class OrderEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    event: str
    actor: str
    details_json: str | None
    created_at: datetime


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    company_id: str
    cart_id: str
    status: str
    customer_name: str
    customer_email: str | None
    customer_phone: str | None
    customer_company: str | None
    delivery_address: str | None
    customer_note: str | None
    estimated_total: Decimal | None
    approved_total: Decimal | None
    currency: str
    company_note: str | None
    approved_by: str | None
    created_at: datetime
    decided_at: datetime | None
    customer_confirmed_at: datetime | None
    events: list[OrderEventRead] = Field(default_factory=list)
