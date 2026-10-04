"""Verify an immutable bundled catalog; never perform network I/O."""

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Catalog:
    metadata: dict
    models: dict
    directory: Path | None = None

    def info(self) -> dict:
        claude = [
            key
            for key, value in self.models.items()
            if "claude" in key.lower() and isinstance(value, dict)
        ]
        return {
            **self.metadata,
            "model_count": len(self.models),
            "claude_entry_count": len(claude),
            "verified": True,
        }


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CatalogError(f"duplicate catalog key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise CatalogError(f"non-finite catalog value: {value}")


def load_catalog(directory: Path | None = None) -> Catalog:
    root = directory if directory is not None else files("claude_metrics").joinpath("data/pricing")
    # Hatch includes data in wheels; editable installs execute from src/.
    checkout = Path(__file__).resolve().parents[3]
    if (
        directory is None
        and not root.joinpath("catalog-metadata.json").is_file()
        and (checkout / "pyproject.toml").is_file()
    ):
        root = checkout / "data" / "pricing"
    try:
        raw = root.joinpath("litellm-model-prices.json").read_bytes()
        metadata = json.loads(
            root.joinpath("catalog-metadata.json").read_text("utf-8"), object_pairs_hook=_object
        )
        if not isinstance(metadata, dict):
            raise CatalogError("catalog metadata must be an object")
        commit = metadata.get("source_commit", "")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise CatalogError("catalog must be pinned to an upstream commit")
        if metadata.get("version") != f"litellm-{commit}":
            raise CatalogError("catalog version must match its pinned commit")
        expected_url = (
            f"https://raw.githubusercontent.com/BerriAI/litellm/{commit}/"
            "model_prices_and_context_window.json"
        )
        if metadata.get("source_url") != expected_url or metadata.get("currency") != "USD":
            raise CatalogError("catalog source or currency is invalid")
        if metadata.get("sha256") != hashlib.sha256(raw).hexdigest():
            raise CatalogError("catalog SHA-256 mismatch")
        if metadata.get("size_bytes") != len(raw):
            raise CatalogError("catalog byte length mismatch")
        models = json.loads(
            raw, parse_float=Decimal, object_pairs_hook=_object, parse_constant=_invalid_constant
        )
        if not isinstance(models, dict) or not models:
            raise CatalogError("catalog must contain model entries")
        for name, model in models.items():
            if name == "sample_spec":
                continue
            if not isinstance(model, dict):
                raise CatalogError(f"invalid model entry: {name}")
            for key, value in model.items():
                if "cost" in key and isinstance(value, (int, Decimal)):
                    if isinstance(value, bool) or value < 0 or not Decimal(value).is_finite():
                        raise CatalogError(f"invalid rate for {name}: {key}")
        return Catalog(metadata, models, Path(str(root)))
    except (OSError, json.JSONDecodeError, UnicodeError, RecursionError, InvalidOperation) as exc:
        raise CatalogError("cannot read or validate catalog") from exc
