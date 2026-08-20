from __future__ import annotations

import unittest

from app.services.chunking import chunk_text, normalize_text


class ChunkingTests(unittest.TestCase):
    def test_normalization_preserves_paragraphs(self) -> None:
        self.assertEqual(normalize_text("A   B\n\n\nC"), "A B\n\nC")

    def test_chunks_preserve_source_location(self) -> None:
        text = "\n\n".join(f"Paragraph {index} " + ("x" * 180) for index in range(10))
        chunks = chunk_text(text, page=7, section="Prices", max_chars=500, overlap_chars=50)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.page == 7 for chunk in chunks))
        self.assertTrue(all(chunk.section == "Prices" for chunk in chunks))
        self.assertEqual([chunk.ordinal for chunk in chunks], list(range(len(chunks))))


if __name__ == "__main__":
    unittest.main()

