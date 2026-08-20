from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.config import Settings, get_settings


@dataclass(slots=True)
class VectorHit:
    chunk_id: str
    score: float
    payload: dict[str, Any]


class QdrantVectorStore:
    def __init__(self, settings: Settings):
        from qdrant_client import QdrantClient

        self.settings = settings
        self.client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key or None,
            timeout=30,
        )
        self.collection = settings.qdrant_collection

    def ensure_collection(self, dimensions: int) -> None:
        from qdrant_client.models import Distance, VectorParams

        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(size=dimensions, distance=Distance.COSINE),
            )
            return
        info = self.client.get_collection(self.collection)
        vectors = info.config.params.vectors
        existing_size = getattr(vectors, "size", None)
        if existing_size and existing_size != dimensions:
            raise RuntimeError(
                f"Qdrant collection has {existing_size} dimensions, but the "
                f"embedding provider returned {dimensions}. Use a new collection name "
                "or recreate the collection."
            )

    def upsert(self, points: list[tuple[str, list[float], dict[str, Any]]]) -> None:
        from qdrant_client.models import PointStruct

        if not points:
            return
        self.ensure_collection(len(points[0][1]))
        self.client.upsert(
            collection_name=self.collection,
            wait=True,
            points=[
                PointStruct(id=point_id, vector=vector, payload=payload)
                for point_id, vector, payload in points
            ],
        )

    def delete_source(self, source_id: str) -> None:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        if not self.client.collection_exists(self.collection):
            return
        self.client.delete(
            collection_name=self.collection,
            wait=True,
            points_selector=Filter(
                must=[FieldCondition(key="source_id", match=MatchValue(value=source_id))]
            ),
        )

    def search(self, company_id: str, vector: list[float], limit: int = 12) -> list[VectorHit]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        if not self.client.collection_exists(self.collection):
            return []
        result = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=Filter(
                must=[FieldCondition(key="company_id", match=MatchValue(value=company_id))]
            ),
            limit=limit,
            with_payload=True,
        )
        return [
            VectorHit(
                chunk_id=str(point.payload.get("chunk_id", point.id)),
                score=float(point.score),
                payload=dict(point.payload or {}),
            )
            for point in result.points
        ]


@lru_cache
def get_vector_store() -> QdrantVectorStore:
    return QdrantVectorStore(get_settings())

