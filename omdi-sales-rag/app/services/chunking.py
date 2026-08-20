from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class TextChunk:
    text: str
    ordinal: int
    page: int | None = None
    section: str | None = None


def normalize_text(value: str) -> str:
    value = value.replace("\x00", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _split_long_block(block: str, max_chars: int) -> list[str]:
    if len(block) <= max_chars:
        return [block]
    sentences = re.split(r"(?<=[.!?])\s+|\n+", block)
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.extend(
                sentence[start : start + max_chars]
                for start in range(0, len(sentence), max_chars)
            )
        elif not current:
            current = sentence
        elif len(current) + len(sentence) + 1 <= max_chars:
            current = f"{current} {sentence}"
        else:
            parts.append(current)
            current = sentence
    if current:
        parts.append(current)
    return parts


def chunk_text(
    text: str,
    *,
    page: int | None = None,
    section: str | None = None,
    max_chars: int = 1400,
    overlap_chars: int = 180,
    start_ordinal: int = 0,
) -> list[TextChunk]:
    clean = normalize_text(text)
    if not clean:
        return []

    blocks: list[str] = []
    for paragraph in re.split(r"\n\s*\n", clean):
        blocks.extend(_split_long_block(paragraph.strip(), max_chars))

    chunks: list[TextChunk] = []
    current = ""
    ordinal = start_ordinal
    for block in blocks:
        if not block:
            continue
        if not current:
            current = block
        elif len(current) + len(block) + 2 <= max_chars:
            current = f"{current}\n\n{block}"
        else:
            chunks.append(
                TextChunk(text=current, ordinal=ordinal, page=page, section=section)
            )
            ordinal += 1
            overlap = current[-overlap_chars:].lstrip() if overlap_chars else ""
            current = f"{overlap}\n\n{block}".strip()
    if current:
        chunks.append(TextChunk(text=current, ordinal=ordinal, page=page, section=section))
    return chunks

