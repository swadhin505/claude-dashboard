"""Application assembly and routes for the loopback-only dashboard."""

import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi import Path as APIPath
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from claude_metrics import reports
from claude_metrics.application import initialize, refresh
from claude_metrics.config import Settings, source_status
from claude_metrics.pricing.policy import PricingPolicy, default_policy
from claude_metrics.runtime import Runtime, default_runtime
from claude_metrics.storage import connect
from claude_metrics.web import schemas
from claude_metrics.web.conversations import register_conversations
from claude_metrics.web.presentation import (
    TOKEN_LABELS,
    bars,
    money,
    money_brief,
    number,
    timestamp,
)
from claude_metrics.web.queries import page_groups, query
from claude_metrics.web.security import install_security

ASSETS = Path(__file__).parent
SessionKey = Annotated[int, APIPath(ge=1, le=2**63 - 1)]


def create_app(
    settings: Settings,
    *,
    policy: PricingPolicy | None = None,
    scan_on_start: bool = True,
    runtime: Runtime | None = None,
) -> FastAPI:
    runtime = default_runtime() if runtime is None else runtime
    policy = default_policy() if policy is None else policy
    catalog = runtime.catalog_loader(settings.data_dir)
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(initialize, settings, catalog, runtime=runtime)
        if scan_on_start:
            app.state.last_refresh = await run_in_threadpool(
                refresh, settings, catalog, policy, runtime=runtime
            )
        yield

    app = FastAPI(
        title="Claude Dashboard",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
        telemetry={
            "tracing": False,
            "metrics": False,
            "logs": False,
            "operation_spans": False,
            "auto_configure": False,
        },
    )
    app.state.last_refresh = None
    templates = Jinja2Templates(directory=ASSETS / "templates")
    templates.env.filters.update(
        money=money, money_brief=money_brief, number=number, timestamp=timestamp
    )
    app.mount("/static", StaticFiles(directory=ASSETS / "static"), name="static")

    install_security(app, token)

    def error(request, message, code):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": message}, status_code=code)
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={"message": message, "code": code},
            status_code=code,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return error(request, str(exc.detail), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error(request, "Invalid request parameters.", 422)

    async def storage_error(request, exc):
        return error(
            request,
            "The local ledger is unavailable. Run claude-metrics doctor "
            "and retry. No source files were changed.",
            503,
        )

    app.add_exception_handler(sqlite3.Error, storage_error)
    app.add_exception_handler(OSError, storage_error)

    @contextmanager
    def snapshot():
        with connect(settings.database, readonly=True) as conn:
            conn.execute("BEGIN")
            yield conn

    def overview_data(conn, q, filters):
        data = reports.overview(conn, filters)
        labels = dict(conn.execute("SELECT project_id,display_name FROM projects"))
        for group in data["projects"]:
            group["label"] = labels.get(group["key"], "Unknown project")
        return page_groups(data, ("daily", "models", "projects", "sources"), q)

    def health_data(conn, q):
        data = page_groups(reports.data_health(conn), ("sources", "incomplete_models"), q)
        data.update(
            roots=source_status(settings),
            parser_version=runtime.source.parser_version,
            formula_version=runtime.pricing.FORMULA_VERSION,
            active_catalog=catalog.info(),
            new_request_policy=asdict(policy),
            last_refresh=app.state.last_refresh,
        )
        return data

    def detail_data(conn, q, filters, session_pk):
        if not conn.execute("SELECT 1 FROM sessions WHERE session_pk=?", (session_pk,)).fetchone():
            raise HTTPException(404, "Session not found in this ledger.")
        return reports.session_detail(conn, filters, session_pk, limit=q.limit, after=q.cursor)

    @app.get("/api/overview", response_model=schemas.Overview)
    def api_overview(request: Request):
        q, filters = query(request, settings)
        with snapshot() as conn:
            return overview_data(conn, q, filters)

    @app.get("/api/sessions", response_model=schemas.Sessions)
    def api_sessions(request: Request):
        q, filters = query(request, settings)
        with snapshot() as conn:
            return reports.sessions(conn, filters, limit=q.limit, after=q.cursor)

    @app.get("/api/sessions/{session_pk}", response_model=schemas.Detail)
    def api_detail(request: Request, session_pk: SessionKey):
        q, filters = query(request, settings)
        with snapshot() as conn:
            return detail_data(conn, q, filters, session_pk)

    @app.get("/api/data-health", response_model=schemas.Health)
    def api_health(request: Request):
        q, _ = query(request, settings)
        with snapshot() as conn:
            return health_data(conn, q)

    @app.post("/api/scan", response_model=schemas.ScanResult)
    def api_scan():
        result = refresh(settings, catalog, policy, runtime=runtime)
        if result["status"] != "busy":
            app.state.last_refresh = result
        return JSONResponse(
            schemas.ScanResult.model_validate(result).model_dump(),
            status_code={"busy": 409, "failed": 503}.get(result["status"], 200),
        )

    def page(request, name, session_pk=None):
        q, filters = query(request, settings)
        with snapshot() as conn:
            health = health_data(conn, q)
            if name == "overview":
                data = schemas.Overview.model_validate(overview_data(conn, q, filters)).model_dump(
                    by_alias=True
                )
            elif name == "sessions":
                data = reports.sessions(conn, filters, limit=q.limit, after=q.cursor)
            elif name == "detail":
                data = detail_data(conn, q, filters, session_pk)
            else:
                data = health
            projects = [
                dict(r)
                for r in conn.execute(
                    "SELECT project_id,display_name FROM projects ORDER BY display_name LIMIT 200"
                )
            ]
            models = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT model_raw FROM requests WHERE superseded_by IS NULL "
                    "ORDER BY model_raw LIMIT 200"
                )
            ]
        base = q.model_dump(by_alias=True, mode="json", exclude_none=True)

        def link(path, **changes):
            params = {**base, "cursor": 0, **changes}
            return path + "?" + urlencode({k: v for k, v in params.items() if v is not None})

        complete = health["last_complete_scan"]
        stale = (
            not complete
            or (int(datetime.now(UTC).timestamp() * 1_000_000) - complete["completed_at_us"])
            > 24 * 60 * 60 * 1_000_000
        )
        return templates.TemplateResponse(
            request=request,
            name=f"{name}.html",
            context={
                "page": name,
                "data": data,
                "health": health,
                "q": q,
                "link": link,
                "projects": projects,
                "models": models,
                "token_labels": TOKEN_LABELS,
                "scan_token": token,
                "stale": stale,
                "bars": bars,
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return page(request, "overview")

    @app.get("/sessions", response_class=HTMLResponse)
    def session_page(request: Request):
        return page(request, "sessions")

    @app.get("/sessions/{session_pk}", response_class=HTMLResponse)
    def detail_page(request: Request, session_pk: SessionKey):
        return page(request, "detail", session_pk)

    @app.get("/data-health", response_class=HTMLResponse)
    def health_page(request: Request):
        return page(request, "health")

    register_conversations(
        app,
        settings=settings,
        templates=templates,
        snapshot=snapshot,
        health_data=health_data,
        token=token,
    )
    return app
