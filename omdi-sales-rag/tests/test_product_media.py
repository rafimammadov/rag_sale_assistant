from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.services.product_media import ProductMediaStore, extract_skus, normalize_sku

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "sample_data" / "yigit-aluminium"


def test_product_code_helpers_are_exact() -> None:
    assert normalize_sku("y6336") == "Y6336"
    assert extract_skus("Please show Y6336 and Y106.1") == ["Y6336", "Y106.1"]


def test_pdf_product_image_is_extracted_by_sku(tmp_path: Path) -> None:
    store = ProductMediaStore(tmp_path, max_images_per_sku=1)

    saved = store.extract_file(
        "company-1",
        SAMPLE_DIR / "led_profiles_price_list_2026-07-24.pdf",
    )
    image_path = store.first_image("company-1", "Y6336")

    assert saved > 0
    assert image_path is not None
    assert image_path.suffix == ".jpg"
    with Image.open(image_path) as image:
        assert image.width >= 220
        assert image.height >= 180


def test_xlsx_product_image_is_extracted_from_anchored_row(tmp_path: Path) -> None:
    store = ProductMediaStore(tmp_path, max_images_per_sku=1)

    saved = store.extract_file(
        "company-1",
        SAMPLE_DIR / "product_image_catalog.xlsx",
    )

    assert saved > 0
    assert store.first_image("company-1", "Y6131") is not None
