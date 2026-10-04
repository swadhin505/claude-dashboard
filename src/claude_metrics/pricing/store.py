"""Explicit commit-pinned catalog installation. Normal reads are always offline."""

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from claude_metrics.ingestion.locking import writer_lock
from claude_metrics.operations import durable_write, operation_log, publish_directory
from claude_metrics.pricing.catalog import Catalog, CatalogError, load_catalog

VERSION = re.compile(r"litellm-[0-9a-f]{40}")
CATALOG_FILES = ("litellm-model-prices.json", "catalog-metadata.json", "LICENSE.litellm")
MAX_DOWNLOAD = 20 * 1024 * 1024


def archived_catalog(data_dir: Path, version: str) -> Catalog:
    if not VERSION.fullmatch(version):
        raise CatalogError("invalid catalog version")
    bundled = load_catalog()
    if version == bundled.metadata["version"]:
        return bundled
    root = data_dir / "pricing" / version
    if not root.is_dir():
        raise CatalogError("archived catalog is unavailable")
    catalog = load_catalog(root)
    if catalog.metadata["version"] != version:
        raise CatalogError("archived catalog identity mismatch")
    return catalog


def active_catalog(data_dir: Path) -> Catalog:
    pointer = data_dir / "pricing" / "active.json"
    if not pointer.exists():
        return load_catalog()
    try:
        selected = json.loads(pointer.read_text("utf-8"))
        if not isinstance(selected, dict) or set(selected) != {"version", "sha256"}:
            raise CatalogError("invalid active catalog pointer")
        catalog = archived_catalog(data_dir, selected["version"])
        if catalog.metadata["sha256"] != selected["sha256"]:
            raise CatalogError("active catalog checksum mismatch")
        return catalog
    except (TypeError, KeyError, json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise CatalogError("invalid active catalog pointer") from exc


def copy_catalog(catalog: Catalog, destination: Path) -> None:
    """Copy validated original bytes; never reserialize Decimal rate values."""
    if catalog.directory is None:
        raise CatalogError("catalog original bytes are unavailable")
    destination.mkdir()
    for name in CATALOG_FILES:
        durable_write(destination / name, (catalog.directory / name).read_bytes())
    copied = load_catalog(destination)
    if copied.metadata != catalog.metadata:
        raise CatalogError("catalog changed while copying")


def archive(catalog: Catalog, directory: Path) -> None:
    destination = directory / catalog.metadata["version"]
    if destination.exists():
        existing = load_catalog(destination)
        if existing.metadata["sha256"] != catalog.metadata["sha256"]:
            raise CatalogError("immutable catalog version collision")
        return
    with tempfile.TemporaryDirectory(prefix=".catalog-stage-", dir=directory) as temporary:
        staged = Path(temporary) / "version"
        copy_catalog(catalog, staged)
        publish_directory(staged, destination)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CatalogError("catalog download redirects are not accepted")


def _download(commit: str) -> bytes:
    url = f"https://raw.githubusercontent.com/BerriAI/litellm/{commit}/model_prices_and_context_window.json"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    deadline = time.monotonic() + 60
    with opener.open(request, timeout=15) as response:
        if (
            response.status != 200
            or response.headers.get("Content-Encoding", "identity") != "identity"
        ):
            raise CatalogError("unexpected catalog response")
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > MAX_DOWNLOAD:
            raise CatalogError("catalog download exceeds size limit")
        chunks, size = [], 0
        while block := response.read(65536):
            size += len(block)
            if size > MAX_DOWNLOAD or time.monotonic() > deadline:
                raise CatalogError("catalog download limit exceeded")
            chunks.append(block)
        if content_length and size != int(content_length):
            raise CatalogError("incomplete catalog download")
        return b"".join(chunks)


def update_catalog(data_dir: Path, commit: str, expected_sha256: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise CatalogError("supply a full 40-character lowercase upstream commit")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise CatalogError("supply the expected 64-character SHA-256 from your reviewed snapshot")
    # Download BEFORE taking the writer lock; only the explicit update command uses network.
    raw = _download(commit)
    if not raw or len(raw) > MAX_DOWNLOAD or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise CatalogError("downloaded catalog size or SHA-256 mismatch")
    with writer_lock(data_dir):
        previous = active_catalog(data_dir)  # never conceal a broken active selection
        directory = data_dir / "pricing"
        directory.mkdir(exist_ok=True)
        bundled = load_catalog()
        with tempfile.TemporaryDirectory(prefix=".catalog-stage-", dir=directory) as temporary:
            staged = Path(temporary) / "candidate"
            staged.mkdir()
            metadata = {
                **bundled.metadata,
                "version": f"litellm-{commit}",
                "source_commit": commit,
                "source_url": f"https://raw.githubusercontent.com/BerriAI/litellm/{commit}/"
                "model_prices_and_context_window.json",
                "sha256": expected_sha256,
                "size_bytes": len(raw),
                "retrieved_at": datetime.now(UTC).isoformat(),
                "source_committed_at": None,
            }
            durable_write(staged / CATALOG_FILES[0], raw)
            durable_write(staged / CATALOG_FILES[1], json.dumps(metadata, indent=2).encode())
            durable_write(
                staged / CATALOG_FILES[2], (bundled.directory / CATALOG_FILES[2]).read_bytes()
            )
            candidate = load_catalog(staged)
            # Units are a fixed supported contract, not guessed from arbitrary new field names.
            anthropic = [
                v
                for v in candidate.models.values()
                if isinstance(v, dict) and v.get("litellm_provider") == "anthropic"
            ]
            from claude_metrics.pricing.calculator import rate

            if not anthropic or not any(
                rate(v.get("input_cost_per_token")) is not None
                and rate(v.get("output_cost_per_token")) is not None
                for v in anthropic
            ):
                raise CatalogError("catalog has no supported Anthropic per-token rate card")
            archive(bundled, directory)
            archive(previous, directory)
            archive(candidate, directory)
            pointer = Path(temporary) / "active.json"
            durable_write(
                pointer,
                json.dumps(
                    {
                        "version": metadata["version"],
                        "sha256": expected_sha256,
                    },
                    sort_keys=True,
                ).encode(),
            )
            os.replace(pointer, directory / "active.json")
        operation_log(data_dir, "catalog_updated")
        return {
            "ok": True,
            "catalog": active_catalog(data_dir).info(),
            "previous_version": previous.metadata["version"],
            "repriced": False,
            "restart_dashboard": True,
        }
