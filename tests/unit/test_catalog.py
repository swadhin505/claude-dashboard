import hashlib
import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from claude_metrics.pricing.catalog import CatalogError, load_catalog

CATALOG_DIR = Path(__file__).resolve().parents[2] / "data/pricing"


def test_upstream_snapshot_is_pinned_exact_and_has_claude_rates():
    catalog = load_catalog()
    info = catalog.info()
    raw = (CATALOG_DIR / "litellm-model-prices.json").read_bytes()
    assert info["sha256"] == hashlib.sha256(raw).hexdigest()
    assert info["size_bytes"] == len(raw)
    assert info["source_commit"] in info["source_url"]
    assert info["historical_schedule"] is False
    assert info["claude_entry_count"] > 0
    assert isinstance(catalog.models["claude-sonnet-4-6"]["input_cost_per_token"], Decimal)
    assert (CATALOG_DIR / info["license_file"]).is_file()


def test_corrupt_catalog_rejected(tmp_path):
    shutil.copytree(CATALOG_DIR, tmp_path / "prices")
    path = tmp_path / "prices/litellm-model-prices.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(CatalogError, match="SHA-256"):
        load_catalog(tmp_path / "prices")


def test_catalog_version_cannot_disagree_with_commit(tmp_path):
    shutil.copytree(CATALOG_DIR, tmp_path / "prices")
    path = tmp_path / "prices/catalog-metadata.json"
    metadata = json.loads(path.read_text("utf-8"))
    metadata["version"] = "../../unsafe"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(CatalogError, match="version"):
        load_catalog(tmp_path / "prices")


@pytest.mark.parametrize(
    "body",
    ['{"x":{},"x":{}}', '{"x":{"input_cost_per_token":NaN}}', '{"x":{"input_cost_per_token":-1}}'],
)
def test_invalid_catalog_even_when_checksum_matches(tmp_path, body):
    metadata = json.loads((CATALOG_DIR / "catalog-metadata.json").read_text("utf-8"))
    data = body.encode()
    metadata.update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    (tmp_path / "litellm-model-prices.json").write_bytes(data)
    (tmp_path / "catalog-metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(CatalogError):
        load_catalog(tmp_path)
