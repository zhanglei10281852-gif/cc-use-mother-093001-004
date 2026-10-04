"""审计日志：所有状态变更动作只追加、不可修改，支持完整查询与序列化。"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional


@dataclass(frozen=True)
class AuditEvent:
    seq: int
    at: datetime
    actor: str
    action: str
    entity_type: str
    entity_id: str
    result: str  # SUCCESS / DENIED
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at.isoformat(),
            "actor": self.actor,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "result": self.result,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuditEvent":
        return cls(
            seq=d["seq"],
            at=datetime.fromisoformat(d["at"]),
            actor=d["actor"],
            action=d["action"],
            entity_type=d["entity_type"],
            entity_id=d["entity_id"],
            result=d["result"],
            details=d.get("details", {}),
        )


class AuditLog:
    def __init__(self, clock: Optional[Callable[[], datetime]] = None) -> None:
        self._events: List[AuditEvent] = []
        self._lock = threading.RLock()
        self._clock = clock or datetime.now

    def append(
        self,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        result: str,
        details: Optional[dict] = None,
    ) -> AuditEvent:
        with self._lock:
            event = AuditEvent(
                seq=len(self._events) + 1,
                at=self._clock(),
                actor=actor,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                result=result,
                details=details or {},
            )
            self._events.append(event)
            return event

    def query(
        self,
        entity_type: Optional[str] = None,
        entity_id: Optional[str] = None,
        action: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> List[AuditEvent]:
        with self._lock:
            out = list(self._events)
        if entity_type:
            out = [e for e in out if e.entity_type == entity_type]
        if entity_id:
            out = [e for e in out if e.entity_id == entity_id]
        if action:
            out = [e for e in out if e.action == action]
        if actor:
            out = [e for e in out if e.actor == actor]
        return out

    def to_list(self) -> List[dict]:
        with self._lock:
            return [e.to_dict() for e in self._events]

    def load(self, events: List[dict]) -> None:
        with self._lock:
            self._events = [AuditEvent.from_dict(d) for d in events]

    def __len__(self) -> int:
        return len(self._events)
