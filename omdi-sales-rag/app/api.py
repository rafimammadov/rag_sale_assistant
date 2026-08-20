from __future__ import annotations

import hmac
import json
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Cart, CartItem, Company, Order, Source
from app.schemas import (
    CartCreate,
    CartItemCreate,
    CartRead,
    ChatRequest,
    ChatResponse,
    CompanyCreate,
    CompanyRead,
    CustomerConfirmation,
    OrderDecision,
    OrderRead,
    OrderSubmit,
    ScrapeRequest,
    ScrapeResult,
    SourceRead,
)
from app.services.chat import ChatService
from app.services.ingestion import IngestionService
from app.services.notifications import NotificationService
from app.services.order_states import (
    CHANGES_REQUESTED,
    COMPANY_APPROVED,
    REJECTED,
)
from app.services.orders import OrderService, add_event
from app.services.product_media import ProductMediaStore
from app.services.scraper import WebsiteScraper
from app.services.security import safe_filename, verify_action_token

router = APIRouter(prefix="/api")
settings = get_settings()


def admin_required(x_admin_api_key: str | None = Header(default=None)) -> None:
    if not x_admin_api_key or not hmac.compare_digest(
        x_admin_api_key,
        settings.admin_api_key,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin key.")


def get_company(db: Session, slug: str) -> Company:
    company = db.scalar(select(Company).where(Company.slug == slug))
    if not company:
        raise HTTPException(status_code=404, detail="Company not found.")
    return company


@router.get("/companies/{slug}/products/{sku}/image", response_class=FileResponse)
def product_image(slug: str, sku: str, db: Session = Depends(get_db)) -> FileResponse:
    company = get_company(db, slug)
    try:
        path = ProductMediaStore(settings.data_dir).first_image(company.id, sku)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Product image not found.") from exc
    if path is None:
        raise HTTPException(status_code=404, detail="Product image not found.")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


def get_company_order(db: Session, company: Company, order_id: str) -> Order:
    order = db.get(Order, order_id)
    if not order or order.company_id != company.id:
        raise HTTPException(status_code=404, detail="Order not found.")
    return order


def authorize_order_review(
    company: Company,
    order: Order,
    *,
    admin_key: str | None,
    review_token: str | None,
) -> None:
    if admin_key and hmac.compare_digest(admin_key, settings.admin_api_key):
        return
    if review_token:
        try:
            payload = verify_action_token(review_token, settings.order_action_secret)
            if (
                payload.get("company_id") == company.id
                and payload.get("order_id") == order.id
                and payload.get("action") == "review"
            ):
                return
        except ValueError:
            pass
    raise HTTPException(status_code=401, detail="Invalid admin key or review token.")


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "app": settings.app_name}


@router.post(
    "/companies",
    response_model=CompanyRead,
    dependencies=[Depends(admin_required)],
)
def create_company(payload: CompanyCreate, db: Session = Depends(get_db)) -> Company:
    if db.scalar(select(Company).where(Company.slug == payload.slug)):
        raise HTTPException(status_code=409, detail="Company slug already exists.")
    company = Company(**payload.model_dump())
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


@router.get(
    "/companies",
    response_model=list[CompanyRead],
    dependencies=[Depends(admin_required)],
)
def list_companies(db: Session = Depends(get_db)) -> list[Company]:
    return list(db.scalars(select(Company).order_by(Company.name)).all())


@router.get("/companies/{slug}", response_model=CompanyRead)
def read_company(slug: str, db: Session = Depends(get_db)) -> Company:
    return get_company(db, slug)


@router.get(
    "/companies/{slug}/sources",
    response_model=list[SourceRead],
    dependencies=[Depends(admin_required)],
)
def list_sources(slug: str, db: Session = Depends(get_db)) -> list[Source]:
    company = get_company(db, slug)
    return list(
        db.scalars(
            select(Source)
            .where(Source.company_id == company.id)
            .order_by(Source.created_at.desc())
        ).all()
    )


@router.post(
    "/companies/{slug}/sources/upload",
    response_model=list[SourceRead],
    dependencies=[Depends(admin_required)],
)
async def upload_sources(
    slug: str,
    files: list[UploadFile] = File(...),
    authority_score: int = Form(default=85, ge=0, le=100),
    db: Session = Depends(get_db),
) -> list[Source]:
    company = get_company(db, slug)
    service = IngestionService()
    results = []
    max_bytes = settings.max_upload_mb * 1024 * 1024
    company_dir = settings.data_dir / "uploads" / company.slug
    company_dir.mkdir(parents=True, exist_ok=True)
    for upload in files:
        filename = safe_filename(upload.filename or "upload")
        extension = Path(filename).suffix.casefold()
        if extension not in settings.allowed_upload_extensions:
            raise HTTPException(status_code=415, detail=f"Unsupported file type: {extension}")
        content = await upload.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"{filename} exceeds the {settings.max_upload_mb} MB limit.",
            )
        path = company_dir / f"{uuid.uuid4()}-{filename}"
        path.write_bytes(content)
        try:
            source, _skipped = service.ingest_file(
                db,
                company_id=company.id,
                path=path,
                display_name=filename,
                origin=f"upload://{filename}",
                authority_score=authority_score,
                metadata={"stored_filename": path.name},
            )
            results.append(source)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Could not index {filename}: {exc}",
            ) from exc
    return results


@router.post(
    "/companies/{slug}/sources/scrape",
    response_model=ScrapeResult,
    dependencies=[Depends(admin_required)],
)
async def scrape_source(
    slug: str,
    payload: ScrapeRequest,
    db: Session = Depends(get_db),
) -> ScrapeResult:
    company = get_company(db, slug)
    crawl = await WebsiteScraper().crawl(payload.url, payload.max_pages, payload.max_depth)
    ingestion = IngestionService()
    indexed = 0
    skipped = crawl.skipped
    failed = list(crawl.failed)
    for page in crawl.pages:
        try:
            _source, duplicate = ingestion.ingest_text(
                db,
                company_id=company.id,
                name=page.title,
                origin=page.url,
                text_content=page.text,
                authority_score=payload.authority_score,
                metadata=page.metadata,
            )
            if duplicate:
                skipped += 1
            else:
                indexed += 1
        except Exception as exc:
            failed.append(f"{page.url}: {exc}")
    return ScrapeResult(
        discovered=crawl.discovered,
        indexed=indexed,
        skipped=skipped,
        failed=failed,
    )


@router.post("/companies/{slug}/chat", response_model=ChatResponse)
def chat(
    slug: str,
    payload: ChatRequest,
    db: Session = Depends(get_db),
) -> ChatResponse:
    company = get_company(db, slug)
    try:
        return ChatService().respond(
            db,
            company=company,
            message=payload.message,
            customer_session=payload.customer_session,
            conversation_id=payload.conversation_id,
            customer_context=payload.customer_context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/companies/{slug}/carts", response_model=CartRead)
def create_cart(
    slug: str,
    payload: CartCreate,
    db: Session = Depends(get_db),
) -> Cart:
    company = get_company(db, slug)
    cart = Cart(company_id=company.id, customer_session=payload.customer_session)
    db.add(cart)
    db.commit()
    db.refresh(cart)
    return cart


@router.get("/companies/{slug}/carts/{cart_id}", response_model=CartRead)
def read_cart(
    slug: str,
    cart_id: str,
    customer_session: str = Query(...),
    db: Session = Depends(get_db),
) -> Cart:
    company = get_company(db, slug)
    cart = db.get(Cart, cart_id)
    if (
        not cart
        or cart.company_id != company.id
        or cart.customer_session != customer_session
    ):
        raise HTTPException(status_code=404, detail="Cart not found.")
    return cart


@router.post("/companies/{slug}/carts/{cart_id}/items", response_model=CartRead)
def add_cart_item(
    slug: str,
    cart_id: str,
    payload: CartItemCreate,
    customer_session: str = Query(...),
    db: Session = Depends(get_db),
) -> Cart:
    company = get_company(db, slug)
    cart = db.get(Cart, cart_id)
    if (
        not cart
        or cart.company_id != company.id
        or cart.customer_session != customer_session
    ):
        raise HTTPException(status_code=404, detail="Cart not found.")
    if cart.status != "draft":
        raise HTTPException(status_code=409, detail="Submitted carts cannot be changed.")
    item_data = payload.model_dump(exclude={"evidence"})
    item = CartItem(
        cart_id=cart.id,
        **item_data,
        evidence_json=json.dumps(payload.evidence, ensure_ascii=False),
    )
    db.add(item)
    db.commit()
    db.refresh(cart)
    return cart


@router.delete("/companies/{slug}/carts/{cart_id}/items/{item_id}", response_model=CartRead)
def remove_cart_item(
    slug: str,
    cart_id: str,
    item_id: str,
    customer_session: str = Query(...),
    db: Session = Depends(get_db),
) -> Cart:
    company = get_company(db, slug)
    cart = db.get(Cart, cart_id)
    item = db.get(CartItem, item_id)
    if (
        not cart
        or cart.company_id != company.id
        or cart.customer_session != customer_session
        or not item
        or item.cart_id != cart.id
    ):
        raise HTTPException(status_code=404, detail="Cart item not found.")
    if cart.status != "draft":
        raise HTTPException(status_code=409, detail="Submitted carts cannot be changed.")
    db.delete(item)
    db.commit()
    db.refresh(cart)
    return cart


@router.post("/companies/{slug}/orders", response_model=OrderRead)
async def submit_order(
    slug: str,
    payload: OrderSubmit,
    db: Session = Depends(get_db),
) -> Order:
    company = get_company(db, slug)
    try:
        order, duplicate = OrderService().submit(db, company=company, payload=payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not duplicate:
        cart = db.get(Cart, order.cart_id)
        try:
            channel = await NotificationService().notify_company(company, order, cart)
            add_event(db, order, "company_notification_sent", "system", {"channel": channel})
            db.commit()
        except Exception as exc:
            add_event(
                db,
                order,
                "company_notification_failed",
                "system",
                {"error": str(exc)[:1000]},
            )
            db.commit()
    db.refresh(order)
    return order


@router.get("/companies/{slug}/orders/{order_id}", response_model=OrderRead)
def read_order(
    slug: str,
    order_id: str,
    customer_session: str = Query(...),
    db: Session = Depends(get_db),
) -> Order:
    company = get_company(db, slug)
    order = get_company_order(db, company, order_id)
    cart = db.get(Cart, order.cart_id)
    if not cart or cart.customer_session != customer_session:
        raise HTTPException(status_code=404, detail="Order not found.")
    return order


@router.get(
    "/admin/companies/{slug}/orders",
    response_model=list[OrderRead],
    dependencies=[Depends(admin_required)],
)
def admin_list_orders(
    slug: str,
    order_status: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
) -> list[Order]:
    company = get_company(db, slug)
    query = select(Order).where(Order.company_id == company.id)
    if order_status:
        query = query.where(Order.status == order_status)
    return list(db.scalars(query.order_by(Order.created_at.desc())).all())


@router.get("/admin/companies/{slug}/orders/{order_id}", response_model=OrderRead)
def admin_read_order(
    slug: str,
    order_id: str,
    x_admin_api_key: str | None = Header(default=None),
    review_token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Order:
    company = get_company(db, slug)
    order = get_company_order(db, company, order_id)
    authorize_order_review(
        company,
        order,
        admin_key=x_admin_api_key,
        review_token=review_token,
    )
    return order


def _review_order(
    *,
    slug: str,
    order_id: str,
    target_status: str,
    decision: OrderDecision,
    admin_key: str | None,
    review_token: str | None,
    db: Session,
) -> Order:
    company = get_company(db, slug)
    order = get_company_order(db, company, order_id)
    authorize_order_review(
        company,
        order,
        admin_key=admin_key,
        review_token=review_token,
    )
    try:
        return OrderService().decide(
            db,
            order=order,
            target_status=target_status,
            decision=decision,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/admin/companies/{slug}/orders/{order_id}/approve", response_model=OrderRead)
def approve_order(
    slug: str,
    order_id: str,
    decision: OrderDecision,
    x_admin_api_key: str | None = Header(default=None),
    review_token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Order:
    return _review_order(
        slug=slug,
        order_id=order_id,
        target_status=COMPANY_APPROVED,
        decision=decision,
        admin_key=x_admin_api_key,
        review_token=review_token,
        db=db,
    )


@router.post("/admin/companies/{slug}/orders/{order_id}/request-changes", response_model=OrderRead)
def request_order_changes(
    slug: str,
    order_id: str,
    decision: OrderDecision,
    x_admin_api_key: str | None = Header(default=None),
    review_token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Order:
    return _review_order(
        slug=slug,
        order_id=order_id,
        target_status=CHANGES_REQUESTED,
        decision=decision,
        admin_key=x_admin_api_key,
        review_token=review_token,
        db=db,
    )


@router.post("/admin/companies/{slug}/orders/{order_id}/reject", response_model=OrderRead)
def reject_order(
    slug: str,
    order_id: str,
    decision: OrderDecision,
    x_admin_api_key: str | None = Header(default=None),
    review_token: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> Order:
    return _review_order(
        slug=slug,
        order_id=order_id,
        target_status=REJECTED,
        decision=decision,
        admin_key=x_admin_api_key,
        review_token=review_token,
        db=db,
    )


@router.post("/companies/{slug}/orders/{order_id}/confirm", response_model=OrderRead)
def confirm_approved_order(
    slug: str,
    order_id: str,
    confirmation: CustomerConfirmation,
    db: Session = Depends(get_db),
) -> Order:
    company = get_company(db, slug)
    order = get_company_order(db, company, order_id)
    cart = db.get(Cart, order.cart_id)
    try:
        return OrderService().customer_confirm(
            db,
            order=order,
            cart=cart,
            confirmation=confirmation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
