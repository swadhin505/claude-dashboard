"""SQL projection of normalized events; identity is never global or content-based."""

import json
import sqlite3
from dataclasses import asdict

from claude_metrics.domain import (
    SQLITE_MAX_INT,
    TOKEN_COLUMNS,
    BillableUnit,
    Project,
    Request,
    Session,
    ToolCall,
    Turn,
)


def identity(request: Request) -> str:
    if request.request_id or request.message_id:
        return json.dumps([request.request_id, request.message_id], separators=(",", ":"))
    source = request.source
    return json.dumps(["source", source.path, source.generation, source.byte_offset])


def candidate(request: Request) -> dict:
    result = asdict(request)
    result.update(result.pop("tokens"))
    for key in ("source", "agent_type", "session_id", "turn_id", "replay_of_session_id"):
        result.pop(key)
    return result


class Ledger:
    def __init__(self, conn: sqlite3.Connection, file_id: int):
        self.conn, self.file_id = conn, file_id
        self.added = self.revised = 0
        self.request_pk: int | None = None
        self.observation_id: int | None = None

    def session(self, sid: str, agent: str = "claude") -> int:
        self.conn.execute(
            "INSERT INTO sessions(agent_type,session_id) VALUES (?,?) ON CONFLICT DO NOTHING",
            (agent, sid),
        )
        return self.conn.execute(
            "SELECT session_pk FROM sessions WHERE agent_type=? AND session_id=?", (agent, sid)
        ).fetchone()[0]

    def turn(self, session_pk: int, turn_id: str | None) -> int | None:
        if turn_id is None:
            return None
        row = self.conn.execute(
            "SELECT turn_pk FROM turns WHERE session_pk=? AND turn_id=?", (session_pk, turn_id)
        ).fetchone()
        return row[0] if row else None

    def apply(self, events: list) -> None:
        self.request_pk = self.observation_id = None
        for event in events:
            if isinstance(event, Project):
                self.conn.execute(
                    """INSERT INTO projects(project_id,agent_type,canonical_root,display_name)
                    VALUES (?,?,?,?) ON CONFLICT DO NOTHING""",
                    (event.project_id, event.agent_type, event.canonical_root, event.display_name),
                )
            elif isinstance(event, Session):
                pk = self.session(event.session_id, event.agent_type)
                self.conn.execute(
                    """UPDATE sessions SET project_id=coalesce(?,project_id),
                    cwd=coalesce(?,cwd), branch=coalesce(?,branch),
                    started_at_us=CASE WHEN started_at_us IS NULL THEN ?
                        WHEN ? IS NULL THEN started_at_us ELSE min(started_at_us,?) END,
                    ended_at_us=CASE WHEN ended_at_us IS NULL THEN ?
                        WHEN ? IS NULL THEN ended_at_us ELSE max(ended_at_us,?) END
                    WHERE session_pk=?""",
                    (
                        event.project_id,
                        event.cwd,
                        event.branch,
                        *([event.started_at_us] * 3),
                        *([event.ended_at_us] * 3),
                        pk,
                    ),
                )
            elif isinstance(event, Turn):
                self.conn.execute(
                    """INSERT INTO turns(session_pk,turn_id,prompt_id,started_at_us,
                    source_file_id,source_line) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (
                        self.session(event.session_id, event.agent_type),
                        event.turn_id,
                        event.prompt_id,
                        event.started_at_us,
                        self.file_id,
                        event.source.line,
                    ),
                )
            elif isinstance(event, Request):
                units = {
                    unit.unit_type: unit.quantity
                    for unit in events
                    if isinstance(unit, BillableUnit)
                }
                self.ingest_request(event, units)
            elif isinstance(event, ToolCall):
                self.ingest_tool(event)
            elif isinstance(event, BillableUnit) and self.observation_id is not None:
                self.conn.execute(
                    """INSERT INTO billable_units(request_pk,observation_id,unit_type,quantity)
                    VALUES (?,?,?,?) ON CONFLICT(observation_id,unit_type)
                    DO UPDATE SET quantity=excluded.quantity""",
                    (self.request_pk, self.observation_id, event.unit_type, event.quantity),
                )

    def find_request(self, sid: int, request: Request):
        exact = self.conn.execute(
            "SELECT * FROM requests WHERE session_pk=? AND dedup_key=? AND superseded_by IS NULL",
            (sid, identity(request)),
        ).fetchone()
        if exact:
            return exact
        if not (request.message_id or request.request_id):
            return None
        # Enrich only when one compatible identity exists. Different non-null IDs never merge.
        rows = self.conn.execute(
            """SELECT * FROM requests WHERE session_pk=? AND superseded_by IS NULL
            AND ((message_id=? AND ? IS NOT NULL) OR (request_id=? AND ? IS NOT NULL))
            AND (request_id IS NULL OR ? IS NULL OR request_id=?)
            AND (message_id IS NULL OR ? IS NULL OR message_id=?)""",
            (
                sid,
                request.message_id,
                request.message_id,
                request.request_id,
                request.request_id,
                request.request_id,
                request.request_id,
                request.message_id,
                request.message_id,
            ),
        ).fetchall()
        return rows[0] if len(rows) == 1 else None

    def owner(self, source_sid: int, request: Request) -> int:
        key = identity(request)
        if request.replay_of_session_id:
            owner = self.session(request.replay_of_session_id, request.agent_type)
            self.conn.execute(
                """INSERT INTO replay_aliases VALUES (?,?,?)
                ON CONFLICT(source_session_pk,identity_key) DO UPDATE
                SET owner_session_pk=excluded.owner_session_pk""",
                (source_sid, key, owner),
            )
            self.conn.execute(
                "UPDATE sessions SET parent_session_pk=?,lineage_evidence='forkedFrom.messageUuid' "
                "WHERE session_pk=?",
                (owner, source_sid),
            )
        owner, visited = source_sid, set()
        while True:
            if owner in visited:
                raise ValueError("cyclic_replay")
            visited.add(owner)
            alias = self.conn.execute(
                "SELECT owner_session_pk FROM replay_aliases WHERE source_session_pk=? "
                "AND identity_key=?",
                (owner, key),
            ).fetchone()
            if not alias:
                return owner
            owner = alias[0]

    def merge(self, old: sqlite3.Row, new_pk: int, owner: int) -> None:
        """Late explicit replay evidence; retain observations and any historical receipts."""
        old_pk = old["request_pk"]
        if old_pk == new_pk:
            return
        self.conn.execute("PRAGMA defer_foreign_keys=ON")
        self.conn.execute(
            "UPDATE request_observations SET is_current_winner=0 WHERE request_pk=?", (old_pk,)
        )
        self.conn.execute(
            "UPDATE cost_receipts SET is_current=0 WHERE request_pk=?",
            (old_pk,),
        )
        self.conn.execute(
            "UPDATE request_observations SET request_pk=? WHERE request_pk=?",
            (new_pk, old_pk),
        )
        self.conn.execute(
            "UPDATE billable_units SET request_pk=? WHERE request_pk=?", (new_pk, old_pk)
        )
        # Inherited tool duplicates are already represented by the original owner's calls.
        self.conn.execute(
            """DELETE FROM tool_calls WHERE request_pk=? AND tool_call_id IN
            (SELECT tool_call_id FROM tool_calls WHERE session_pk=?)""",
            (old_pk, owner),
        )
        self.conn.execute(
            "UPDATE tool_calls SET request_pk=?,session_pk=?,turn_pk=NULL WHERE request_pk=?",
            (new_pk, owner, old_pk),
        )
        archived = self.conn.execute(
            "SELECT 1 FROM cost_receipts WHERE request_pk=? UNION ALL "
            "SELECT 1 FROM requests WHERE superseded_by=? LIMIT 1",
            (old_pk, old_pk),
        ).fetchone()
        if archived:
            self.conn.execute(
                "UPDATE requests SET superseded_by=? WHERE request_pk=?", (new_pk, old_pk)
            )
        else:
            self.conn.execute("DELETE FROM requests WHERE request_pk=?", (old_pk,))
        self.conn.execute(
            """UPDATE requests SET revision=max(revision,
            (SELECT coalesce(max(request_revision),1) FROM request_observations WHERE request_pk=?))
            WHERE request_pk=?""",
            (new_pk, new_pk),
        )

    def ingest_request(self, request: Request, units: dict[str, int]) -> None:
        source_sid = self.session(request.session_id, request.agent_type)
        owner = self.owner(source_sid, request)
        # A retained source occurrence is stronger evidence than an incomplete identifier.
        # Replaying it after a second retry appears must not create an ambiguous third request.
        row = self.conn.execute(
            """SELECT r.* FROM request_observations o JOIN requests r USING(request_pk)
            WHERE o.source_file_id=? AND o.generation=? AND o.byte_offset=?
            AND r.session_pk=? ORDER BY o.observation_id DESC LIMIT 1""",
            (self.file_id, request.source.generation, request.source.byte_offset, owner),
        ).fetchone()
        if row is None:
            row = self.find_request(owner, request)
        if row is None:
            cursor = self.conn.execute(
                """INSERT INTO requests(session_pk,dedup_key,request_id,message_id,
                occurred_at_us,model_raw,identity_confidence) VALUES (?,?,?,?,?,?,?)""",
                (
                    owner,
                    identity(request),
                    request.request_id,
                    request.message_id,
                    request.occurred_at_us,
                    request.model_raw,
                    "high" if request.message_id or request.request_id else "low",
                ),
            )
            pk = cursor.lastrowid
            self.added += 1
        else:
            pk = row["request_pk"]
            rid = row["request_id"] or request.request_id
            mid = row["message_id"] or request.message_id
            if rid != row["request_id"] or mid != row["message_id"]:
                self.conn.execute(
                    "UPDATE requests SET request_id=?,message_id=?,dedup_key=? WHERE request_pk=?",
                    (rid, mid, json.dumps([rid, mid], separators=(",", ":")), pk),
                )
        if owner != source_sid:
            previous = self.find_request(source_sid, request)
            if previous:
                self.merge(previous, pk, owner)
        data = candidate(request)
        data["billable_units"] = units
        data["turn_pk"] = self.turn(owner, request.turn_id) if owner == source_sid else None
        # Units are stored separately, but remain tied to this observation, not summed revisions.
        rank = min(SQLITE_MAX_INT, sum(data[k] or 0 for k in TOKEN_COLUMNS))
        src = request.source
        self.conn.execute(
            """INSERT INTO request_observations(request_pk,request_revision,source_file_id,
            source_session_pk,generation,source_line,byte_offset,parser_version,
            normalized_candidate_json,observed_request_id,observed_message_id,finality,usage_rank)
            VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT
            (source_file_id,generation,byte_offset,parser_version) DO NOTHING""",
            (
                pk,
                self.file_id,
                source_sid,
                src.generation,
                src.line,
                src.byte_offset,
                src.parser_version,
                json.dumps(data, sort_keys=True),
                request.request_id,
                request.message_id,
                request.is_final,
                rank,
            ),
        )
        obs = self.conn.execute(
            """SELECT observation_id FROM request_observations WHERE source_file_id=?
            AND generation=? AND byte_offset=? AND parser_version=?""",
            (self.file_id, src.generation, src.byte_offset, src.parser_version),
        ).fetchone()[0]
        self.request_pk, self.observation_id = pk, obs
        self.project_request(pk)

    def project_request(self, pk: int) -> None:
        rows = self.conn.execute(
            """SELECT o.*,f.canonical_path FROM request_observations o
            JOIN source_files f ON f.file_id=o.source_file_id
            WHERE o.request_pk=? AND o.superseded=0""",
            (pk,),
        ).fetchall()
        if not rows:
            return
        normalized = {o["observation_id"]: json.loads(o["normalized_candidate_json"]) for o in rows}
        winner = max(
            rows,
            key=lambda o: (
                o["finality"] == 1,
                sum(normalized[o["observation_id"]][k] or 0 for k in TOKEN_COLUMNS),
                o["source_line"],
                o["canonical_path"],
                o["generation"],
                o["parser_version"],
            ),
        )
        data = dict(normalized[winner["observation_id"]])
        data.pop("is_final")
        units = data.pop("billable_units")
        current = self.conn.execute("SELECT * FROM requests WHERE request_pk=?", (pk,)).fetchone()
        data["request_id"] = current["request_id"] or data["request_id"]
        data["message_id"] = current["message_id"] or data["message_id"]
        # A duplicate subagent file can supply classification the parent copy lacked.
        evidence = list(normalized.values())
        data["occurred_at_us"] = min(
            current["occurred_at_us"], *(d["occurred_at_us"] for d in evidence)
        )
        sub = next((d for d in evidence if d["query_source"] == "subagent"), None)
        if sub:
            data["query_source"], data["agent_id"] = "subagent", sub["agent_id"]
        if winner["source_session_pk"] != current["session_pk"]:
            data["turn_pk"] = None
        if data["turn_pk"] is None:
            data["turn_pk"] = current["turn_pk"]
        old_winner = self.conn.execute(
            "SELECT normalized_candidate_json,observation_id FROM request_observations "
            "WHERE request_pk=? AND is_current_winner=1",
            (pk,),
        ).fetchone()
        had_winner = old_winner is not None
        previous_units = json.loads(old_winner[0]).get("billable_units", {}) if old_winner else {}
        changed = (
            any(current[k] != v for k, v in data.items())
            or units != previous_units
            or (old_winner is not None and old_winner[1] != winner["observation_id"])
        )
        revision = current["revision"] + int(changed and had_winner)
        self.revised += int(changed and had_winner)
        fields = ",".join(f"{key}=?" for key in data)
        self.conn.execute(
            f"UPDATE requests SET {fields},revision=? WHERE request_pk=?",
            (*data.values(), revision, pk),
        )
        if changed or not winner["is_current_winner"]:
            self.conn.execute("UPDATE cost_receipts SET is_current=0 WHERE request_pk=?", (pk,))
        self.conn.execute(
            "UPDATE request_observations SET is_current_winner=0 WHERE request_pk=?", (pk,)
        )
        self.conn.execute(
            "UPDATE request_observations SET is_current_winner=1,request_revision=? "
            "WHERE observation_id=?",
            (revision, winner["observation_id"]),
        )

    def ingest_tool(self, event: ToolCall) -> None:
        sid = self.session(event.session_id, event.agent_type)
        if self.request_pk:
            sid = self.conn.execute(
                "SELECT session_pk FROM requests WHERE request_pk=?", (self.request_pk,)
            ).fetchone()[0]
        self.conn.execute(
            """INSERT INTO tool_calls(session_pk,tool_call_id,request_pk,turn_pk,tool_name,
            success,duration_ms,source_file_id,source_line) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_pk,tool_call_id) DO UPDATE SET
            request_pk=coalesce(excluded.request_pk,tool_calls.request_pk),
            turn_pk=coalesce(excluded.turn_pk,tool_calls.turn_pk),
            tool_name=CASE WHEN excluded.tool_name='unknown' THEN tool_calls.tool_name
                ELSE excluded.tool_name END,
            success=coalesce(excluded.success,tool_calls.success),
            duration_ms=coalesce(excluded.duration_ms,tool_calls.duration_ms)""",
            (
                sid,
                event.tool_call_id,
                self.request_pk,
                self.turn(sid, event.turn_id),
                event.name,
                event.success,
                event.duration_ms,
                self.file_id,
                event.source.line,
            ),
        )


def summary(conn: sqlite3.Connection) -> dict:
    result = {
        "requests": 0,
        "tokens": dict.fromkeys(TOKEN_COLUMNS, 0),
        "unknown_counters": dict.fromkeys(TOKEN_COLUMNS, 0),
        "known_tokens": 0,
        "incomplete_requests": 0,
        "by_source": {},
    }
    # Python integer aggregation avoids SQLite SUM overflow for a large retained ledger.
    for row in conn.execute("SELECT * FROM requests WHERE superseded_by IS NULL"):
        result["requests"] += 1
        incomplete = False
        known = 0
        for key in TOKEN_COLUMNS:
            if row[key] is None:
                result["unknown_counters"][key] += 1
                incomplete = True
            else:
                result["tokens"][key] += row[key]
                known += row[key]
        result["known_tokens"] += known
        result["incomplete_requests"] += int(incomplete)
        partition = result["by_source"].setdefault(
            row["query_source"], {"requests": 0, "known_tokens": 0}
        )
        partition["requests"] += 1
        partition["known_tokens"] += known
    result["all_tokens"] = None if result["incomplete_requests"] else result["known_tokens"]
    return result
