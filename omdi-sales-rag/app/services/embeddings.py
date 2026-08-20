from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

from app.config import Settings, get_settings


class EmbeddingProvider(Protocol):
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class HashEmbeddingProvider:
    """Offline deterministic demo embeddings.

    This hashed bag-of-words representation is useful for smoke tests and exact
    multilingual terminology/SKU matching. Use a real multilingual embedding
    model for production semantic retrieval.
    """

    def __init__(self, dimensions: int = 384):
        self.dimensions = dimensions

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = re.findall(r"[\w.-]+", text.casefold(), flags=re.UNICODE)
        features = tokens + [
            f"{token[:4]}*"
            for token in tokens
            if len(token) >= 6
        ]
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=16).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)


class OpenAIEmbeddingProvider:
    def __init__(self, settings: Settings):
        from openai import OpenAI

        self.dimensions = settings.embedding_dimensions
        self.model = settings.embedding_model
        self.client = OpenAI(
            api_key=settings.embedding_api_key or "not-set",
            base_url=settings.embedding_base_url or None,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        response = self.client.embeddings.create(model=self.model, input=texts)
        ordered = sorted(response.data, key=lambda item: item.index)
        vectors = [list(item.embedding) for item in ordered]
        if vectors:
            self.dimensions = len(vectors[0])
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    settings = get_settings()
    provider = settings.embedding_provider.casefold()
    if provider == "hash":
        return HashEmbeddingProvider(settings.embedding_dimensions)
    if provider in {"openai", "openai-compatible"}:
        return OpenAIEmbeddingProvider(settings)
    raise ValueError(f"Unsupported embedding provider: {settings.embedding_provider}")

