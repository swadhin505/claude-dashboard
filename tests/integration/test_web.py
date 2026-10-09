import json
import re
import shutil
import sqlite3
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from time import perf_counter

import pytest
from fastapi.testclient import TestClient

from claude_metrics.cli import main
from claude_metrics.config import Settings
from claude_metrics.ingestion.locking import writer_lock
from claude_metrics.ingestion.scanner import scan
from claude_metrics.pricing.calculator import PricingPolicy
from claude_metrics.pricing.receipts import apply_pricing, verify_receipts
from claude_metrics.reports import ReportFilter, overview, session_detail, sessions
from claude_metrics.storage import connect
from claude_metrics.web.app import create_app
from claude_metrics.web.presentation import money

FIXTURES = Path(__file__).parents[1] / "fixtures/claude"
ORIGIN = "http://127.0.0.1:8765"
ESTIMATE = PricingPolicy(assume_provider="anthropic")
CASES = json.loads((Path(__file__).parents[1] / "golden/claude-cases.json").read_text())["cases"]


@pytest.fixture
def settings(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    return Settings(tmp_path / "data", (root,), "Asia/Kolkata")


def fixture(settings, name="normal.jsonl"):
    shutil.copyfile(FIXTURES / name, settings.source_dirs[0] / name)


def client(settings, **kwargs):
    return TestClient(create_app(settings, **kwargs), base_url=ORIGIN)


def headers(web):
    html = web.get("/").text
    token = re.search(r'name="scan-token" content="([^"]+)"', html)[1]
    return {"Origin": ORIGIN, "X-Scan-Token": token}


def test_api_and_pages_match_shared_reports(settings):
    fixture(settings)
    with client(settings, policy=ESTIMATE) as web:
        with connect(settings.database, readonly=True) as conn:
            expected = overview(conn, ReportFilter(settings.timezone))
            expected_sessions = sessions(conn, ReportFilter(settings.timezone), limit=30)
            expected_detail = session_detail(conn, ReportFilter(settings.timezone), 1, limit=30)
        actual = web.get("/api/overview").json()
        assert actual["total"] == expected["total"]
        assert actual["total"]["known_cost_nanos"] == 1_620_000
        assert actual["total"]["all_tokens"] == 450
        assert web.get("/api/sessions").json() == expected_sessions
        assert web.get("/api/sessions/1").json() == expected_detail
        for path in ("/", "/sessions", "/sessions/1", "/data-health"):
            response = web.get(path)
            assert response.status_code == 200, response.text
            assert "API-equivalent" in response.text or path == "/data-health"
            assert "SECRET_" not in response.text
            assert response.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        html = web.get("/").text
        assert "$0.00162" in html and "estimated" in html
        assert "project" in html
        assert web.get("/api/data-health").status_code == 200
        assert web.get("/static/dashboard.css").status_code == 200
        assert web.get("/static/dashboard.js").status_code == 200
        assert web.get("/openapi.json").status_code == 200
        assert web.get("/docs").status_code == 404  # no remotely hosted Swagger assets


def test_strict_unknown_provider_is_not_displayed_as_free(settings):
    fixture(settings)
    with client(settings, policy=PricingPolicy()) as web:
        data = web.get("/api/overview").json()["total"]
        assert data["cost_status"] == "UNPRICED"
        assert data["total_cost_nanos"] is None and data["unpriced_tokens"] == 450
        html = web.get("/").text
        assert 'class="cost-value">Unpriced' in html
        assert "450 known tokens without rates" in html
        assert "unknown_or_unsupported_provider" in html


def test_default_dashboard_prices_new_requests_and_preserves_refresh(settings):
    fixture(settings)
    with client(settings) as web:
        before = web.get("/api/overview").json()["total"]
        assert before["total_cost_nanos"] == 1_620_000  # explicit 1h writes stay 1h
        assert before["estimated_requests"] == 1
        policy = web.get("/api/data-health").json()["new_request_policy"]
        assert policy["assume_provider"] == "anthropic" and policy["assume_cache_5m"]
        assert 'class="badge estimated"' in web.get("/").text
        assert web.post("/api/scan", headers=headers(web)).json()["pricing"]["receipts_added"] == 0
        assert web.get("/api/overview").json()["total"] == before
        with connect(settings.database, readonly=True) as conn:
            assert verify_receipts(conn)["ok"]


@pytest.mark.parametrize("mode,provider", [("auto", "anthropic"), ("strict", None)])
def test_serve_passes_selected_cost_mode(settings, monkeypatch, mode, provider):
    import uvicorn

    def inspect_app(app, **kwargs):
        with TestClient(app, base_url=ORIGIN) as web:
            policy = web.get("/api/data-health").json()["new_request_policy"]
            assert policy["assume_provider"] == provider
            assert policy["assume_cache_5m"] is (mode == "auto")

    monkeypatch.setattr(uvicorn, "run", inspect_app)
    assert (
        main(["serve", "--data-dir", str(settings.data_dir), "--no-scan", "--cost-mode", mode]) == 0
    )


def test_empty_missing_sources_and_no_scan(settings):
    settings.source_dirs[0].rmdir()
    with client(settings, scan_on_start=False) as web:
        assert web.get("/api/overview").json()["total"]["requests"] == 0
        html = web.get("/").text
        assert "No matching usage yet" in html and "Source missing" in html
        assert "No scan history" in html
        response = web.post("/api/scan", headers=headers(web))
        assert response.status_code == 200
        assert response.json()["scan"]["diagnostics"]
        assert "retained source diagnostics" in web.get("/").text


@pytest.mark.parametrize(
    "params",
    [
        "from=2026-01-01",
        "to=2026-01-01",
        "from=2026-02-02&to=2026-01-01",
        "from=9999-12-31&to=9999-12-31",
        "tz=Wrong/Timezone",
        "limit=0",
        "limit=201",
        "cursor=-1",
        "cursor=9223372036854775808",
        "limit=1&limit=2",
        "surprise=yes",
        "from=not-a-date&to=2026-01-01",
    ],
)
def test_invalid_filters_have_safe_errors(settings, params):
    with client(settings, scan_on_start=False) as web:
        assert web.get("/api/overview?" + params).status_code == 422
        response = web.get("/?" + params)
        assert response.status_code == 422 and "Unable to show this report" in response.text


def test_date_filters_and_navigation_keep_state(settings):
    fixture(settings, "midnight.jsonl")
    with client(settings, policy=ESTIMATE) as web:
        query = "from=2026-09-30&to=2026-09-30&tz=Asia%2FKolkata&limit=1"
        data = web.get("/api/overview?" + query).json()
        with connect(settings.database, readonly=True) as conn:
            expected = overview(
                conn, ReportFilter("Asia/Kolkata", date(2026, 9, 30), date(2026, 9, 30))
            )
        assert data["total"] == expected["total"]
        assert data["total"]["requests"] == 1
        html = web.get("/sessions?" + query).text
        assert "from=2026-09-30&amp;to=2026-09-30&amp;tz=Asia%2FKolkata" in html
        assert "2026-10-01" not in web.get("/api/sessions?" + query).text


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "127.0.0.1.evil.example",
        "0.0.0.0",
        "localhost:0",
        "localhost:99999",
        "user@localhost",
        "localhost.",
    ],
)
def test_dns_rebinding_hosts_rejected(settings, host):
    with client(settings, scan_on_start=False) as web:
        assert web.get("/api/overview", headers={"Host": host}).status_code == 400


def test_scan_requires_origin_and_random_token_and_has_no_cors(settings):
    with client(settings, scan_on_start=False) as web:
        valid = headers(web)
        for invalid in (
            {},
            {"Origin": ORIGIN},
            {"X-Scan-Token": valid["X-Scan-Token"]},
            {**valid, "Origin": "null"},
            {**valid, "Origin": "https://evil.example"},
            {**valid, "Origin": "http://localhost:8765"},
            {**valid, "Origin": "http://127.0.0.1:9999"},
            {**valid, "X-Scan-Token": "wrong"},
            {**valid, "Sec-Fetch-Site": "cross-site"},
        ):
            response = web.post("/api/scan", headers=invalid)
            assert response.status_code == 403
            assert "access-control-allow-origin" not in response.headers
        assert web.get("/api/scan").status_code == 405
        assert (
            web.post(
                "/api/scan", headers=[(b"origin", ORIGIN.encode()), (b"x-scan-token", b"\xff")]
            ).status_code
            == 403
        )
        assert (
            web.post("/api/scan", headers=[*valid.items(), ("Origin", ORIGIN)]).status_code == 403
        )
        assert web.get("/", headers={"Origin": "http://evil.example"}).status_code == 403
        assert web.post("/api/scan", headers=valid).status_code == 200


def test_writer_busy_and_refresh_idempotent(settings):
    fixture(settings)
    with client(settings, policy=ESTIMATE) as web:
        valid = headers(web)
        before = web.get("/api/overview").json()
        with writer_lock(settings.data_dir):
            response = web.post("/api/scan", headers=valid)
            assert response.status_code == 409 and response.json()["status"] == "busy"
            assert web.get("/api/overview").json() == before
        assert web.post("/api/scan", headers=valid).json()["pricing"]["receipts_added"] == 0
        assert web.get("/api/overview").json() == before


def test_revision_preserves_last_selected_not_last_created_policy(settings):
    fixture(settings)
    scan(settings)
    apply_pricing(settings, policy=PricingPolicy())
    apply_pricing(settings, policy=ESTIMATE, reprice=True)
    apply_pricing(settings, policy=PricingPolicy(), reprice=True)  # select older strict receipt
    item = json.loads((FIXTURES / "normal.jsonl").read_text().splitlines()[1])
    item["message"]["usage"]["output_tokens"] = 60
    with (settings.source_dirs[0] / "normal.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item) + "\n")
    with client(settings, policy=ESTIMATE) as web:
        assert web.get("/api/overview").json()["total"]["cost_status"] == "UNPRICED"
        receipt = web.get("/api/sessions/1").json()["requests"][0]["receipt"]
        assert receipt["policy"]["assume_provider"] is None
        with connect(settings.database, readonly=True) as conn:
            assert verify_receipts(conn)["ok"]


def test_external_scan_ambiguous_policy_stays_pending(settings):
    fixture(settings)
    scan(settings)
    apply_pricing(settings)
    apply_pricing(settings, policy=ESTIMATE, reprice=True)
    item = json.loads((FIXTURES / "normal.jsonl").read_text().splitlines()[1])
    item["message"]["usage"]["output_tokens"] = 60
    with (settings.source_dirs[0] / "normal.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item) + "\n")
    scan(settings)
    with client(settings) as web:
        assert web.get("/api/overview").json()["total"]["warnings"] == {"pricing_pending": 1}
        result = web.get("/api/data-health").json()["last_refresh"]
        assert result["pricing"]["policy_required_requests"] == 1
        assert not result["ok"]


def test_errors_are_sanitized_and_retained_data_available(settings, monkeypatch):
    fixture(settings)
    with client(settings, policy=ESTIMATE) as web:
        import claude_metrics.application as service

        def fail(*args, **kwargs):
            raise ValueError("SECRET_prompt_or_path")

        monkeypatch.setattr(service, "_apply_pricing_locked", fail)
        response = web.post("/api/scan", headers=headers(web))
        assert response.status_code == 503 and "SECRET_" not in response.text
        assert "needs attention" in web.get("/").text
        assert web.get("/api/sessions/999999").status_code == 404
        assert web.get("/sessions/not-an-integer").status_code == 422
        import claude_metrics.web.app as app_module

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("SECRET_database_path")

        monkeypatch.setattr(app_module.reports, "overview", broken)
        response = web.get("/")
        assert response.status_code == 503 and "SECRET_" not in response.text


def test_xss_metadata_is_escaped_and_assets_are_local(settings):
    item = json.loads((FIXTURES / "normal.jsonl").read_text().splitlines()[1])
    item["message"]["model"] = '<script>alert("x")</script>'
    (settings.source_dirs[0] / "xss.jsonl").write_text(json.dumps(item) + "\n", encoding="utf-8")
    with client(settings) as web:
        html = web.get("/").text
        assert '<script>alert("x")</script>' not in html
        assert "&lt;script&gt;" in html
        assert not re.search(r'(?:src|href)="https?://', html)
        script = web.get("/static/dashboard.js").text
        assert "cost_nanos" not in script and "input_tokens" not in script
        assert "innerHTML" not in script


def test_large_history_pagination_and_server_grouping(settings):
    template = json.loads((FIXTURES / "normal.jsonl").read_text().splitlines()[1])
    with (settings.source_dirs[0] / "large.jsonl").open("w", encoding="utf-8") as handle:
        for i in range(2500):
            item = {
                **template,
                "sessionId": f"session-{i // 50}",
                "uuid": f"uuid-{i}",
                "requestId": f"req-{i}",
                "message": {**template["message"], "id": f"msg-{i}"},
            }
            handle.write(json.dumps(item) + "\n")
    with client(settings, policy=ESTIMATE) as web:
        started = perf_counter()
        data = web.get("/api/overview?limit=1").json()
        assert data["total"]["requests"] == 2500
        assert len(data["daily"]) == 1 and "requests" not in data
        first = web.get("/api/sessions?limit=30").json()
        second = web.get(f"/api/sessions?limit=30&cursor={first['next_cursor']}").json()
        assert len(first["items"]) == 30 and len(second["items"]) == 20
        assert not {s["session_pk"] for s in first["items"]} & {
            s["session_pk"] for s in second["items"]
        }
        detail = web.get("/api/sessions/1?limit=20").json()
        assert len(detail["requests"]) == 20 and detail["total"]["requests"] == 50
        following = web.get(f"/api/sessions/1?limit=20&cursor={detail['next_cursor']}").json()
        assert len(following["requests"]) == 20
        assert web.get("/").status_code == 200
        elapsed = perf_counter() - started
        print(f"2500-request HTTP overview + paged sessions/detail + HTML: {elapsed:.3f}s")
        assert elapsed < 20  # generous regression guard, not a production SLA


def test_serve_is_fixed_to_loopback_and_telemetry_disabled(settings, monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    assert main(["serve", "--no-scan", "--port", "8767"]) == 0
    app, kwargs = calls[0]
    assert kwargs == {
        "host": "127.0.0.1",
        "port": 8767,
        "workers": 1,
        "proxy_headers": False,
        "access_log": False,
    }
    assert app._telemetry["auto_configure"] is False
    assert app._telemetry["logs"] is False
    assert main(["serve", "--port", "0"]) == 1


def test_exact_display_even_above_javascript_safe_integer():
    assert money(9_007_199_254_740_993) == "$9,007,199.254740993"
    assert money(1) == "$0.000000001"
    assert money(None) == "Unavailable"
    assert money(0) == "$0.00"


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_every_truth_fixture_through_http(settings, case):
    for name in case["files"]:
        destination = settings.source_dirs[0] / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURES / name, destination)
    with client(settings, policy=ESTIMATE) as web:
        result = web.get("/api/overview").json()
        assert result["total"]["requests"] == case["expected"]["requests"]
        assert result["total"]["tokens"] == case["expected"]["tokens"]
        with connect(settings.database, readonly=True) as conn:
            assert result["total"] == overview(conn, ReportFilter(settings.timezone))["total"]
        for path in ("/", "/sessions", "/data-health"):
            assert web.get(path).status_code == 200
        for item in web.get("/api/sessions").json()["items"]:
            assert web.get(f"/sessions/{item['session_pk']}").status_code == 200
        if result["total"]["cost_status"] == "PARTIAL":
            assert "known portion" in web.get("/").text


def test_group_pages_and_exact_model_project_filter(settings):
    fixture(settings, "mixed-models.jsonl")
    with client(settings, policy=ESTIMATE) as web:
        first = web.get("/api/overview?limit=1").json()
        second = web.get(f"/api/overview?limit=1&cursor={first['next_cursor']}").json()
        assert first["total"] == second["total"]
        assert first["models"][0]["key"] != second["models"][0]["key"]
        assert len(first["models"]) == len(second["models"]) == 1
        project = first["projects"][0]["key"]
        result = web.get(
            "/api/overview", params={"project": project, "model": "claude-haiku-4-5"}
        ).json()
        assert result["total"]["requests"] == 1
        assert result["total"]["known_cost_nanos"] == 70_000
        assert web.get("/api/overview?model=missing").json()["total"]["requests"] == 0
        assert web.get("/api/sessions/9223372036854775808").status_code == 422
        assert web.get("/api/sessions/-1").status_code == 422


def test_stale_banner_and_unchanged_counts(settings):
    fixture(settings)
    with client(settings) as web:
        result = web.post("/api/scan", headers=headers(web)).json()
        assert result["scan"]["files_unchanged"] == 1
        assert result["scan"]["files_processed"] == 0
        with connect(settings.database) as conn:
            conn.execute("UPDATE ingestion_runs SET started_at_us=0,completed_at_us=1")
        assert "Snapshot may be stale" in web.get("/").text


class PageElements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


@pytest.mark.parametrize("path", ["/", "/sessions", "/sessions/1", "/data-health"])
def test_redesigned_pages_keep_accessible_local_structure(settings, path):
    fixture(settings)
    with client(settings) as web:
        html = web.get(path).text
        elements = PageElements(html).elements
        assert sum(tag == "h1" for tag, _ in elements) == 1
        # Session detail has two independent navigation sets: primary and session views.
        assert sum(attrs.get("aria-current") == "page" for _, attrs in elements) == (
            2 if path == "/sessions/1" else 1
        )
        ids = [attrs["id"] for _, attrs in elements if "id" in attrs]
        assert len(ids) == len(set(ids))
        assert "main" in ids and "scan-status" in ids
        assert ("a", {"class": "skip", "href": "#main"}) in elements
        assert any(attrs.get("aria-live") == "polite" for _, attrs in elements)
        assert all("style" not in attrs for _, attrs in elements)
        assert all(attrs.get("scope") == "col" for tag, attrs in elements if tag == "th")
        assert all(not name.lower().startswith("on") for _, attrs in elements for name in attrs)
        assert "No conversation content stored" in html
        assert "not a subscription bill" in html
        if path != "/data-health":
            assert "&lt;$0.01" in html  # tiny positive costs never appear as free
            assert "$0.00162" in html  # full precision is still rendered
            assert any(
                tag == "details" and attrs.get("class") == "cost-detail" for tag, attrs in elements
            )
        assert all(
            attrs.get("aria-hidden") == "true" for tag, attrs in elements if tag == "svg"
        )  # visual bars are redundant with the readable exact-count table


def test_native_filters_expand_when_applied_and_keep_get_navigation(settings):
    fixture(settings)
    with client(settings) as web:
        for query, expanded in (("", False), ("?model=claude-sonnet-4-5", True)):
            elements = PageElements(web.get("/" + query).text).elements
            panel = next(
                attrs
                for tag, attrs in elements
                if tag == "details" and attrs.get("class") == "filter-panel"
            )
            assert ("open" in panel) is expanded
            assert any(tag == "form" and attrs.get("method") == "get" for tag, attrs in elements)
            for field in ("from", "to", "tz", "project", "model", "limit"):
                assert any(tag == "input" and attrs.get("name") == field for tag, attrs in elements)


def test_unavailable_source_notice_is_expanded(settings):
    settings.source_dirs[0].rmdir()
    with client(settings, scan_on_start=False) as web:
        elements = PageElements(web.get("/").text).elements
        assert any(
            tag == "details" and attrs.get("class") == "notice data-notices" and "open" in attrs
            for tag, attrs in elements
        )
