"""Conversation-only HTTP orchestration; accounting routes and filters stay independent."""

from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from claude_metrics.transcripts.service import conversation, sources
from claude_metrics.web import schemas


class ConversationQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_id: int | None = Field(default=None, ge=1, le=2**63 - 1)
    page: int = Field(default=1, ge=1, le=2**31 - 1)


def register_conversations(app, *, settings, templates, snapshot, health_data, token):
    def load(request, session_pk):
        if not 1 <= session_pk <= 2**63 - 1:
            raise HTTPException(422, "Invalid session key.")
        params = dict(request.query_params)
        if len(params) != len(request.query_params.multi_items()):
            raise HTTPException(422, "Repeated query parameters are not supported.")
        try:
            q = ConversationQuery.model_validate(params)
        except ValidationError:
            raise HTTPException(422, "Use a positive page and a session-linked file_id.") from None
        try:
            with snapshot() as conn:
                session, choices = sources(conn, session_pk)
            # Do not hold a DB snapshot while reading/formatting potentially large source files.
            return conversation(
                session, choices, settings.source_dirs, file_id=q.file_id, page=q.page
            )
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from None

    @app.get("/api/sessions/{session_pk}/conversation")
    def api_conversation(request: Request, session_pk: int):
        return load(request, session_pk)

    @app.get("/sessions/{session_pk}/conversation", response_class=HTMLResponse)
    def conversation_page(request: Request, session_pk: int):
        data = load(request, session_pk)
        q = schemas.Query(tz=settings.timezone)
        with snapshot() as conn:
            health = health_data(conn, q)

        def conversation_link(*, file_id=data["file_id"], page=1):
            params = {"page": page}
            if file_id is not None:
                params["file_id"] = file_id
            return f"/sessions/{session_pk}/conversation?" + urlencode(params)

        return templates.TemplateResponse(
            request=request,
            name="conversation.html",
            context={
                "page": "conversation",
                "data": data,
                "health": health,
                "q": q,
                "link": lambda path: path,
                "conversation_link": conversation_link,
                "scan_token": token,
                "stale": False,
            },
        )
