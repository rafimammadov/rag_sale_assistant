from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Chunk, Source
from app.services.chunking import TextChunk, chunk_text
from app.services.embeddings import EmbeddingProvider, get_embedding_provider
from app.services.parsers import ParsedSection, parse_file
from app.services.product_media import ProductMediaStore
from app.services.vector_store import QdrantVectorStore, get_vector_store


class IngestionService:
    def __init__(
        self,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: QdrantVectorStore | None = None,
        media_store: ProductMediaStore | None = None,
    ):
        self.embedding_provider = embedding_provider or get_embedding_provider()
        self.vector_store = vector_store or get_vector_store()
        self.media_store = media_store or ProductMediaStore(get_settings().data_dir)

    def _extract_media(self, company_id: str, path: Path) -> None:
        try:
            self.media_store.extract_file(company_id, path)
        except Exception:
            # Media is optional and must never make document indexing fail.
            return

    @staticmethod
    def _checksum(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    def _prepare_source(
        self,
        db: Session,
        *,
        company_id: str,
        kind: str,
        name: str,
        origin: str,
        checksum: str,
        authority_score: int,
        metadata: dict[str, Any] | None,
    ) -> tuple[Source, bool]:
        existing = db.scalar(
            select(Source).where(
                Source.company_id == company_id,
                Source.checksum == checksum,
                Source.origin == origin,
            )
        )
        if existing and existing.status == "ready":
            return existing, True
        if existing:
            db.execute(text("DELETE FROM chunks_fts WHERE company_id = :company_id AND chunk_id IN "
                            "(SELECT id FROM chunks WHERE source_id = :source_id)"),
                       {"company_id": company_id, "source_id": existing.id})
            db.execute(delete(Chunk).where(Chunk.source_id == existing.id))
            existing.status = "processing"
            existing.error = None
            existing.name = name
            existing.kind = kind
            existing.authority_score = authority_score
            existing.metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
            db.commit()
            self.vector_store.delete_source(existing.id)
            return existing, False

        source = Source(
            company_id=company_id,
            kind=kind,
            name=name,
            origin=origin,
            checksum=checksum,
            authority_score=authority_score,
            status="processing",
            metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
        )
        db.add(source)
        db.commit()
        db.refresh(source)
        return source, False

    @staticmethod
    def _make_chunks(sections: list[ParsedSection]) -> list[TextChunk]:
        output: list[TextChunk] = []
        ordinal = 0
        for section in sections:
            section_chunks = chunk_text(
                section.text,
                page=section.page,
                section=section.section,
                start_ordinal=ordinal,
            )
            output.extend(section_chunks)
            ordinal += len(section_chunks)
        return output

    def _index_sections(
        self,
        db: Session,
        source: Source,
        sections: list[ParsedSection],
    ) -> Source:
        try:
            prepared = self._make_chunks(sections)
            if not prepared:
                raise ValueError("The source contained no indexable text.")
            rows = [
                Chunk(
                    company_id=source.company_id,
                    source_id=source.id,
                    ordinal=part.ordinal,
                    page=part.page,
                    section=part.section,
                    text=part.text,
                )
                for part in prepared
            ]
            db.add_all(rows)
            db.flush()
            embeddings = self.embedding_provider.embed_documents([row.text for row in rows])
            if len(embeddings) != len(rows):
                raise RuntimeError("Embedding provider returned an unexpected number of vectors.")
            points = [
                (
                    row.id,
                    vector,
                    {
                        "chunk_id": row.id,
                        "company_id": source.company_id,
                        "source_id": source.id,
                        "page": row.page,
                        "section": row.section,
                    },
                )
                for row, vector in zip(rows, embeddings, strict=True)
            ]
            self.vector_store.upsert(points)
            for row in rows:
                db.execute(
                    text(
                        "INSERT INTO chunks_fts(chunk_id, company_id, text) "
                        "VALUES (:chunk_id, :company_id, :text)"
                    ),
                    {"chunk_id": row.id, "company_id": source.company_id, "text": row.text},
                )
            source.status = "ready"
            source.error = None
            db.commit()
            db.refresh(source)
            return source
        except Exception as exc:
            db.rollback()
            source = db.get(Source, source.id)
            if source:
                source.status = "failed"
                source.error = str(exc)[:2000]
                db.commit()
            raise

    def ingest_file(
        self,
        db: Session,
        *,
        company_id: str,
        path: Path,
        display_name: str,
        origin: str,
        authority_score: int = 80,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Source, bool]:
        content = path.read_bytes()
        source, skipped = self._prepare_source(
            db,
            company_id=company_id,
            kind="document",
            name=display_name,
            origin=origin,
            checksum=self._checksum(content),
            authority_score=authority_score,
            metadata=metadata,
        )
        if skipped:
            self._extract_media(company_id, path)
            return source, True
        indexed = self._index_sections(db, source, parse_file(path))
        self._extract_media(company_id, path)
        return indexed, False

    def ingest_text(
        self,
        db: Session,
        *,
        company_id: str,
        name: str,
        origin: str,
        text_content: str,
        authority_score: int = 70,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[Source, bool]:
        source, skipped = self._prepare_source(
            db,
            company_id=company_id,
            kind="website",
            name=name,
            origin=origin,
            checksum=self._checksum(text_content.encode("utf-8")),
            authority_score=authority_score,
            metadata=metadata,
        )
        if skipped:
            return source, True
        sections = [ParsedSection(text=text_content, section=name, metadata=metadata or {})]
        return self._index_sections(db, source, sections), False
