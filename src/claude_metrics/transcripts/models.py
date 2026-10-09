"""In-memory display types. Never passed to the ledger or pricing engine."""

from dataclasses import dataclass, field


@dataclass
class Result:
    text: str
    status: str
    line: int
    timestamp: str | None


@dataclass
class Block:
    kind: str
    text: str
    tool_id: str | None = None
    name: str | None = None
    results: list[Result] = field(default_factory=list)


@dataclass
class Message:
    role: str
    line: int
    last_line: int
    timestamp: str | None
    uuid: str | None
    parent_uuid: str | None
    message_id: str | None
    request_id: str | None
    model: str | None
    stream: str
    blocks: list[Block]
    ended: bool = False


@dataclass
class Transcript:
    messages: list[Message] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
