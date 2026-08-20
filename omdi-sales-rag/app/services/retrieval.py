from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import Chunk, Source
from app.services.embeddings import EmbeddingProvider, get_embedding_provider
from app.services.vector_store import QdrantVectorStore, get_vector_store


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: str
    text: str
    source_name: str
    origin: str
    authority_score: int
    page: int | None
    section: str | None
    score: float


_TURKISH_FOLD = str.maketrans(
    {
        "ç": "c",
        "ğ": "g",
        "ı": "i",
        "ö": "o",
        "ş": "s",
        "ü": "u",
    }
)

_CATALOG_OVERVIEW_PATTERNS = (
    r"\bwhat (?:products|product categories|product groups) do you (?:have|offer|sell)\b",
    r"\bwhat do you (?:have|offer|sell)\b",
    r"\bwhich products\b",
    r"\bshow (?:me )?(?:your )?(?:products|catalogue|catalog)\b",
    r"\bproduct (?:range|families|categories|groups|catalogue|catalog)\b",
    r"\b(?:elinizde|sizde) (?:hangi |ne )?urun(?:ler|leriniz)?\b",
    r"\bhangi urun(?:ler)?(?: var)?\b",
    r"\bne (?:tur )?urun(?:ler)? (?:var|satiyorsunuz|sunuyorsunuz)\b",
    r"\bneler (?:var|satiyorsunuz|sunuyorsunuz)\b",
    r"\burun (?:gruplari|kategorileri|yelpazesi|katalogu)\b",
)

_OVERVIEW_SOURCE_MARKERS = (
    "product families",
    "product categories",
    "product groups",
    "product range",
    "urun gruplari",
    "urun kategorileri",
    "kategorilerim",
    "katalog",
)


def fold_for_matching(value: str) -> str:
    folded = value.casefold().translate(_TURKISH_FOLD)
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", folded)
        if not unicodedata.combining(character)
    )


def is_catalog_overview_query(query: str) -> bool:
    folded = fold_for_matching(query)
    return any(re.search(pattern, folded) for pattern in _CATALOG_OVERVIEW_PATTERNS)


def expand_retrieval_query(
    query: str,
    *,
    catalog_overview: bool | None = None,
) -> str:
    if catalog_overview is None:
        catalog_overview = is_catalog_overview_query(query)
    if not catalog_overview:
        return query
    return (
        f"{query}\n"
        "product families product categories product range product catalog "
        "ürün grupları ürün kategorileri ürün yelpazesi ürün kataloğu"
    )


def build_fts_query(query: str) -> str:
    tokens = re.findall(r"[\w.-]{2,}", query.casefold(), flags=re.UNICODE)
    unique = list(dict.fromkeys(tokens))[:16]
    return " OR ".join(f'"{token.replace(chr(34), "")}"*' for token in unique)


class RetrievalService:
    def __init__(
        self,
        embedding_provider: EmbeddingProvider | None = None,
        vector_store: QdrantVectorStore | None = None,
    ):
        self.embedding_provider = embedding_provider or get_embedding_provider()
        self.vector_store = vector_store or get_vector_store()

    def retrieve(
        self,
        db: Session,
        *,
        company_id: str,
        query: str,
        limit: int = 8,
        catalog_overview: bool | None = None,
    ) -> list[RetrievedChunk]:
        ranked: dict[str, float] = {}
        overview_query = (
            is_catalog_overview_query(query)
            if catalog_overview is None
            else catalog_overview
        )
        retrieval_query = expand_retrieval_query(
            query,
            catalog_overview=overview_query,
        )
        try:
            vector = self.embedding_provider.embed_query(retrieval_query)
            vector_hits = self.vector_store.search(company_id, vector, limit=max(limit * 2, 12))
            for rank, hit in enumerate(vector_hits, start=1):
                similarity = max(0.0, min(float(hit.score), 1.0))
                ranked[hit.chunk_id] = (
                    ranked.get(hit.chunk_id, 0.0)
                    + 1.0 / (60 + rank)
                    + similarity * 0.015
                )
        except Exception:
            # Exact FTS retrieval remains available if the vector service is temporarily down.
            vector_hits = []

        fts_query = build_fts_query(retrieval_query)
        if fts_query:
            try:
                rows = db.execute(
                    text(
                        """
                        SELECT chunks_fts.chunk_id, bm25(chunks_fts) AS lexical_rank
                        FROM chunks_fts
                        JOIN chunks c ON c.id = chunks_fts.chunk_id
                        JOIN sources s ON s.id = c.source_id
                        WHERE chunks_fts MATCH :query
                          AND chunks_fts.company_id = :company_id
                          AND s.status = 'ready'
                        ORDER BY lexical_rank
                        LIMIT :limit
                        """
                    ),
                    {"query": fts_query, "company_id": company_id, "limit": max(limit * 2, 12)},
                ).all()
                for rank, row in enumerate(rows, start=1):
                    ranked[str(row.chunk_id)] = ranked.get(str(row.chunk_id), 0.0) + 1.0 / (
                        60 + rank
                    )
            except Exception:
                pass

        skus = re.findall(r"\b[A-Za-z]\d+(?:\.\d+)?\b", query)
        if skus:
            conditions = " OR ".join(f"c.text LIKE :sku_{index}" for index in range(len(skus)))
            params = {"company_id": company_id, "limit": max(limit, 10)}
            params.update({f"sku_{index}": f"%{sku}%" for index, sku in enumerate(skus)})
            rows = db.execute(
                text(
                    f"""
                    SELECT c.id
                    FROM chunks c
                    JOIN sources s ON s.id = c.source_id
                    WHERE c.company_id = :company_id AND s.status = 'ready'
                      AND ({conditions})
                    LIMIT :limit
                    """
                ),
                params,
            ).all()
            for rank, row in enumerate(rows, start=1):
                ranked[str(row.id)] = ranked.get(str(row.id), 0.0) + 2.0 / (60 + rank)

        if not ranked:
            return []
        candidate_ids = list(ranked)
        records = db.execute(
            select(Chunk, Source)
            .join(Source, Source.id == Chunk.source_id)
            .where(Chunk.id.in_(candidate_ids), Source.status == "ready")
        ).all()
        output = []
        for chunk, source in records:
            authority_boost = 0.006 * max(0, min(source.authority_score, 100)) / 100
            overview_boost = 0.0
            if overview_query:
                searchable = fold_for_matching(
                    " ".join(
                        value
                        for value in (source.name, chunk.section, chunk.text)
                        if value
                    )
                )
                if any(marker in searchable for marker in _OVERVIEW_SOURCE_MARKERS):
                    overview_boost = 0.05
            output.append(
                RetrievedChunk(
                    chunk_id=chunk.id,
                    text=chunk.text,
                    source_name=source.name,
                    origin=source.origin,
                    authority_score=source.authority_score,
                    page=chunk.page,
                    section=chunk.section,
                    score=ranked.get(chunk.id, 0.0) + authority_boost + overview_boost,
                )
            )
        output.sort(key=lambda item: item.score, reverse=True)
        return output[:limit]
