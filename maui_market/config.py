from __future__ import annotations

from pathlib import Path

import yaml

from maui_market.models import ComplexConfig

CONFIG_DIR = Path(__file__).resolve().parent / "config"
PACKAGE_ROOT = Path(__file__).resolve().parent


def load_complex_config(slug: str) -> ComplexConfig:
    path = CONFIG_DIR / f"{slug}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Complex config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ComplexConfig(
        slug=data["slug"],
        name=data["name"],
        source=data.get("source", "redfin"),
        search_url=data["search_url"],
        address_pattern=data.get(
            "address_pattern",
            r"(?i)(?:unit|apt|apartment|#)\s*([A-Z]-?\d{2,4}|[A-Z]{1,2}\d{2,3})\b",
        ),
        street_number=str(data.get("street_number", "")),
        street_name=data.get("street_name", "Kihei"),
        tmks_file=data.get("tmks_file", ""),
        data_root=data.get("data_root", "data"),
        output_prefix=data.get("output_prefix", data.get("slug", slug).replace("_", "-")),
    )


def list_complex_slugs() -> list[str]:
    return sorted(path.stem for path in CONFIG_DIR.glob("*.yaml"))
