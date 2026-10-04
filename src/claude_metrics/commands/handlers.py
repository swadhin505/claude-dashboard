"""Execute CLI commands using the existing application services."""

import argparse

from claude_metrics import __version__
from claude_metrics.application import initialize
from claude_metrics.config import Settings, load_settings, source_status
from claude_metrics.ingestion.scanner import scan
from claude_metrics.maintenance import backup, support_bundle
from claude_metrics.pricing.policy import (
    DEFAULT_DISCREPANCY_TOLERANCE_NANOS,
    PricingPolicy,
    policy_for_mode,
)
from claude_metrics.pricing.receipts import apply_pricing, verify_receipts
from claude_metrics.pricing.store import update_catalog
from claude_metrics.reports import ReportFilter, data_health, overview, session_detail, sessions
from claude_metrics.runtime import default_runtime
from claude_metrics.storage import connect, inspect_database
from claude_metrics.units import date_bounds


def _settings(args: argparse.Namespace) -> Settings:
    return load_settings(
        config_file=args.config,
        data_dir=args.data_dir,
        source_dirs=args.source_dir,
        timezone=args.timezone,
    )


def doctor(settings: Settings) -> dict:
    result = {
        "version": __version__,
        "phase": 5,
        "timezone": settings.timezone,
        "database_path": str(settings.database),
        "sources": [],
        "warnings": [],
        "ok": True,
    }
    result["sources"] = source_status(settings)
    for source in result["sources"]:
        if source["status"] != "available":
            result["warnings"].append(f"source {source['status']}: {source['path']}")
    result["catalog"] = default_runtime().catalog_loader(settings.data_dir).info()
    if settings.database.is_file():
        with connect(settings.database, readonly=True) as conn:
            result["database"] = inspect_database(conn)
        result["ok"] = result["database"]["ok"]
    else:
        result["database"] = {"status": "not_initialized"}
        result["warnings"].append("database is not initialized; run claude-metrics init")
    result["capabilities"] = {"ingestion": True, "request_pricing": True, "dashboard": True}
    return result


def _pricing_policy(args: argparse.Namespace) -> PricingPolicy:
    return policy_for_mode(
        args.cost_mode,
        assume_provider=args.assume_provider,
        assume_cache_5m=args.assume_cache_5m,
        discrepancy_tolerance_nanos=getattr(
            args, "discrepancy_tolerance_nanos", DEFAULT_DISCREPANCY_TOLERANCE_NANOS
        ),
    )


def _serve(settings: Settings, args: argparse.Namespace) -> None:
    if not 1 <= args.port <= 65535:
        raise ValueError("port must be 1..65535")
    import uvicorn

    from claude_metrics.web.app import create_app

    app = create_app(
        settings,
        scan_on_start=not args.no_scan,
        policy=_pricing_policy(args),
    )
    print(f"Claude Dashboard: http://127.0.0.1:{args.port} (Ctrl+C to stop)")
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=args.port,
        workers=1,
        proxy_headers=False,
        access_log=False,
    )


def _apply_prices(settings: Settings, args: argparse.Namespace) -> dict:
    filters = ReportFilter(settings.timezone, args.start, args.end)
    filters.where()  # validate paired local dates
    start, end = (
        date_bounds(args.start, args.end, settings.timezone) if args.start else (0, 2**63 - 1)
    )
    return apply_pricing(
        settings,
        reprice=args.reprice,
        start_us=start,
        end_us=end,
        policy=_pricing_policy(args),
    )


def _read_report(settings: Settings, args: argparse.Namespace) -> dict:
    """Keep schema checks and all report queries in one read-only snapshot."""
    with connect(settings.database, readonly=True) as conn:
        if not inspect_database(conn)["schema_current"]:
            raise ValueError("database needs migration; run claude-metrics init")
        conn.execute("BEGIN")
        if args.command == "pricing":
            return verify_receipts(conn, data_dir=settings.data_dir)
        if args.report_command == "health":
            return data_health(conn)
        filters = ReportFilter(settings.timezone, args.start, args.end, args.project, args.model)
        filters.where()
        if args.report_command == "overview":
            return overview(conn, filters)
        if args.report_command == "sessions":
            return sessions(conn, filters, limit=args.limit, after=args.after)
        return session_detail(conn, filters, args.session_pk, limit=args.limit, after=args.after)


def execute(args: argparse.Namespace) -> dict | None:
    """Return a command result, or None after the interactive server exits."""
    settings = _settings(args)
    if args.command in ("backup", "support-bundle"):
        operation = backup if args.command == "backup" else support_bundle
        return operation(settings, args.output)
    if args.command == "pricing":
        if args.pricing_command == "info":
            return default_runtime().catalog_loader(settings.data_dir).info()
        if args.pricing_command == "update":
            return update_catalog(settings.data_dir, args.commit, args.sha256)
        if args.pricing_command == "apply":
            return _apply_prices(settings, args)
        return _read_report(settings, args)
    if args.command == "serve":
        _serve(settings, args)
        return None
    if args.command == "init":
        return initialize(settings)
    if args.command == "scan":
        return scan(settings, full=args.full)
    if args.command == "report":
        return _read_report(settings, args)
    return doctor(settings)
