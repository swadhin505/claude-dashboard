"""Install the loopback, same-origin, CSRF, and response-header boundary."""

import re
import secrets

from fastapi import FastAPI
from fastapi.responses import JSONResponse


def install_security(app: FastAPI, token: str) -> None:
    @app.middleware("http")
    async def local_boundary(request, call_next):
        hosts = request.headers.getlist("host")
        host = hosts[0] if len(hosts) == 1 else ""
        match = re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?", host)
        origin = f"http://{host}"
        if not match or (match[1] and not 1 <= int(match[1]) <= 65535):
            response = JSONResponse({"detail": "Local Host required."}, status_code=400)
        elif request.headers.get("sec-fetch-site") == "cross-site" or (
            "origin" in request.headers and request.headers["origin"] != origin
        ):
            response = JSONResponse({"detail": "Same-origin access required."}, status_code=403)
        elif request.method not in ("GET", "HEAD", "OPTIONS") and (
            request.headers.get("origin") != origin
            or len(request.headers.getlist("origin")) != 1
            or len(request.headers.getlist("x-scan-token")) != 1
            or not secrets.compare_digest(
                request.headers.get("x-scan-token", "").encode("utf-8"), token.encode("ascii")
            )
        ):
            response = JSONResponse({"detail": "Same-origin scan token required."}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; "
                "style-src 'self'; "
                "connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                "form-action 'self'",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cross-Origin-Resource-Policy": "same-origin",
            }
        )
        return response
