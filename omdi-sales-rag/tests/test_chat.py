from __future__ import annotations

import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import Company
from app.services.chat import ChatService


class StubRetrieval:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def retrieve(self, _db: Session, **kwargs):
        self.calls.append(kwargs)
        return []


class StubProvider:
    def __init__(self) -> None:
        self.system_prompt = ""
        self.user_prompt = ""

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return json.dumps(
            {
                "answer": (
                    "We offer LED profiles, PVC wall-panel profiles, skirting profiles, "
                    "and plasterboard profiles [S1]."
                ),
                "recommendations": [],
                "next_question": "What will you use the profiles for?",
                "should_offer_quote": False,
            }
        )


def test_catalog_overview_uses_company_profile_and_stays_category_level() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    retrieval = StubRetrieval()
    provider = StubProvider()

    with Session(engine) as db:
        company = Company(
            name="Example Aluminium",
            slug="example-aluminium",
            assistant_name="Sales Assistant",
            currency="TRY",
            description=(
                "Produces LED profiles, PVC wall-panel profiles, skirting profiles, "
                "and plasterboard profiles."
            ),
        )
        db.add(company)
        db.commit()

        response = ChatService(retrieval=retrieval, provider=provider).respond(
            db,
            company=company,
            message="Elinizde hangi urunler var?",
            customer_session="telegram:100:200",
            conversation_id=None,
            customer_context={"channel": "telegram"},
        )

    assert retrieval.calls[0]["catalog_overview"] is True
    assert retrieval.calls[0]["limit"] == 6
    assert "Request type:\ncatalog_overview" in provider.user_prompt
    assert "Configured company profile" in provider.user_prompt
    assert len(response.sources) == 1
    assert response.sources[0].source_name == "Example Aluminium company profile"
    assert response.recommendations == []
    assert response.commercial_confirmation_required is True
