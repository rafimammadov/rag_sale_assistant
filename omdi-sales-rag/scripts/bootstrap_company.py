from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Company
from app.schemas import CompanyCreate
from app.services.ingestion import IngestionService
from app.services.scraper import WebsiteScraper


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


async def scrape_company(db, company: Company, config: dict) -> None:
    website = config.get("company", {}).get("website") or config.get("website")
    if not website:
        return
    crawl_config = config.get("crawl", {})
    output = await WebsiteScraper().crawl(
        website,
        max_pages=int(crawl_config.get("max_pages", 40)),
        max_depth=int(crawl_config.get("max_depth", 3)),
    )
    ingestion = IngestionService()
    indexed = 0
    skipped = output.skipped
    for page in output.pages:
        try:
            _source, duplicate = ingestion.ingest_text(
                db,
                company_id=company.id,
                name=page.title,
                origin=page.url,
                text_content=page.text,
                authority_score=int(crawl_config.get("authority_score", 70)),
                metadata=page.metadata,
            )
            skipped += int(duplicate)
            indexed += int(not duplicate)
        except Exception as exc:
            output.failed.append(f"{page.url}: {exc}")
    print(
        f"Website crawl: indexed={indexed}, skipped={skipped}, "
        f"failed={len(output.failed)}"
    )
    for failure in output.failed[:10]:
        print(f"  - {failure}")


def bootstrap(config_path: Path, scrape: bool) -> None:
    init_db()
    config = load_config(config_path)
    company_payload = CompanyCreate.model_validate(config["company"])
    source_dir = config_path.parent

    with SessionLocal() as db:
        company = db.scalar(select(Company).where(Company.slug == company_payload.slug))
        if company is None:
            company = Company(**company_payload.model_dump())
            db.add(company)
        else:
            for key, value in company_payload.model_dump().items():
                setattr(company, key, value)
        db.commit()
        db.refresh(company)
        print(f"Company ready: {company.name} ({company.slug})")

        ingestion = IngestionService()
        for source_config in config.get("sources", []):
            path = source_dir / source_config["file"]
            if not path.exists():
                print(f"Missing source, skipped: {path}")
                continue
            try:
                source, duplicate = ingestion.ingest_file(
                    db,
                    company_id=company.id,
                    path=path,
                    display_name=source_config.get("name", path.name),
                    origin=f"sample://{path.name}",
                    authority_score=int(source_config.get("authority_score", 80)),
                    metadata={"sample": True, "notes": source_config.get("notes")},
                )
                label = "already indexed" if duplicate else "indexed"
                print(f"{label}: {source.name} ({source.status})")
            except Exception as exc:
                print(f"failed: {path.name}: {exc}")

        if scrape:
            asyncio.run(scrape_company(db, company, config))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a company and index its starter data.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("sample_data/yigit-aluminium/company.json"),
    )
    parser.add_argument(
        "--scrape",
        action="store_true",
        help="Also crawl the configured website after indexing local files.",
    )
    args = parser.parse_args()
    bootstrap(args.config.resolve(), args.scrape)


if __name__ == "__main__":
    main()
