from __future__ import annotations

import unittest
from pathlib import Path

from app.services.parsers import parse_pdf, parse_xlsx


SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data" / "yigit-aluminium"


class ParserTests(unittest.TestCase):
    def test_led_pdf_keeps_page_level_price_evidence(self) -> None:
        sections = parse_pdf(SAMPLE_DIR / "led_profiles_price_list_2026-07-24.pdf")
        self.assertGreaterEqual(len(sections), 55)
        page_six = next(section for section in sections if section.page == 6)
        self.assertIn("Y6131", page_six.text)
        self.assertIn("85 TL", page_six.text)

    def test_pvc_pdf_keeps_color_price_variants(self) -> None:
        sections = parse_pdf(SAMPLE_DIR / "pvc_wall_panel_price_list_2026-07-23.pdf")
        outside_corner = next(section for section in sections if section.page == 3)
        self.assertIn("Y6190", outside_corner.text)
        self.assertIn("107 TL", outside_corner.text)
        self.assertIn("113 TL", outside_corner.text)

    def test_xlsx_is_normalized_into_sku_rows(self) -> None:
        sections = parse_xlsx(SAMPLE_DIR / "product_image_catalog.xlsx")
        self.assertGreaterEqual(len(sections), 55)
        y6317 = next(section for section in sections if "Y6317" in section.text)
        self.assertIn("SIVA ÜSTÜ TAVAN LED PROFİLİ", y6317.text)


if __name__ == "__main__":
    unittest.main()

