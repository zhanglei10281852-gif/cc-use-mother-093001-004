"""SQLite 持久化：区段、方案版本、许可、幂等键与审计哈希链。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS sections (
    section_id TEXT PRIMARY KEY,
    doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    doc TEXT NOT NULL,
    PRIMARY KEY (plan_id, version)
);
CREATE TABLE IF NOT EXISTS permits (
    permit_id TEXT PRIMARY KEY,
    doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    details TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""

GENESIS = "GENESIS"


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- 自增编号 ----
    def next_id(self, prefix: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (f"seq_{prefix}",)
            ).fetchone()
            n = (row["value"] if row else 0) + 1
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (f"seq_{prefix}", n),
            )
            self._conn.commit()
            return f"{prefix}-{n:04d}"

    # ---- 区段 ----
    def put_section(self, doc: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sections(section_id, doc) VALUES(?, ?) "
                "ON CONFLICT(section_id) DO UPDATE SET doc = excluded.doc",
                (doc["section_id"], canonical(doc)),
            )
            self._conn.commit()

    def get_section(self, section_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT doc FROM sections WHERE section_id = ?", (section_id,)
        ).fetchone()
        return json.loads(row["doc"]) if row else None

    def list_sections(self) -> list:
        rows = self._conn.execute("SELECT doc FROM sections ORDER BY section_id").fetchall()
        return [json.loads(r["doc"]) for r in rows]

    # ---- 方案（按版本） ----
    def put_plan(self, doc: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO plans(plan_id, version, doc) VALUES(?, ?, ?) "
                "ON CONFLICT(plan_id, version) DO UPDATE SET doc = excluded.doc",
                (doc["plan_id"], doc["version"], canonical(doc)),
            )
            self._conn.commit()

    def get_plan(self, plan_id: str, version: int | None = None) -> dict | None:
        if version is None:
            row = self._conn.execute(
                "SELECT doc FROM plans WHERE plan_id = ? ORDER BY version DESC LIMIT 1",
                (plan_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT doc FROM plans WHERE plan_id = ? AND version = ?",
                (plan_id, version),
            ).fetchone()
        return json.loads(row["doc"]) if row else None

    def list_plans(self, status: str | None = None) -> list:
        """每个方案的最新版本；可按状态过滤。"""
        rows = self._conn.execute(
            "SELECT p.doc FROM plans p "
            "JOIN (SELECT plan_id, MAX(version) v FROM plans GROUP BY plan_id) m "
            "ON p.plan_id = m.plan_id AND p.version = m.v"
        ).fetchall()
        docs = [json.loads(r["doc"]) for r in rows]
        if status is not None:
            docs = [d for d in docs if d.get("status") == status]
        return docs

    # ---- 许可 ----
    def put_permit(self, doc: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO permits(permit_id, doc) VALUES(?, ?) "
                "ON CONFLICT(permit_id) DO UPDATE SET doc = excluded.doc",
                (doc["permit_id"], canonical(doc)),
            )
            self._conn.commit()

    def get_permit(self, permit_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT doc FROM permits WHERE permit_id = ?", (permit_id,)
        ).fetchone()
        return json.loads(row["doc"]) if row else None

    def list_permits(self, status_in: tuple | None = None) -> list:
        rows = self._conn.execute("SELECT doc FROM permits ORDER BY permit_id").fetchall()
        docs = [json.loads(r["doc"]) for r in rows]
        if status_in is not None:
            docs = [d for d in docs if d.get("status") in status_in]
        return docs

    # ---- 幂等 ----
    def get_idempotency(self, key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM idempotency WHERE key = ?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def put_idempotency(self, key: str, actor: str, request_hash: str, response: dict, ts: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO idempotency(key, actor, request_hash, response, created_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (key, actor, request_hash, canonical(response), ts),
            )
            self._conn.commit()

    # ---- 审计（哈希链） ----
    def append_audit(self, ts: str, actor: str, action: str,
                     entity_type: str, entity_id: str, details: dict) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT hash FROM audit ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["hash"] if row else GENESIS
            payload = canonical({
                "ts": ts, "actor": actor, "action": action,
                "entity_type": entity_type, "entity_id": entity_id,
                "details": details, "prev_hash": prev_hash,
            })
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            cur = self._conn.execute(
                "INSERT INTO audit(ts, actor, action, entity_type, entity_id, details, prev_hash, hash) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, actor, action, entity_type, entity_id, canonical(details), prev_hash, digest),
            )
            self._conn.commit()
            return {
                "seq": cur.lastrowid, "ts": ts, "actor": actor, "action": action,
                "entity_type": entity_type, "entity_id": entity_id,
                "details": details, "prev_hash": prev_hash, "hash": digest,
            }

    def list_audit(self, entity_type: str | None = None, entity_id: str | None = None) -> list:
        sql = "SELECT * FROM audit"
        cond, args = [], []
        if entity_type:
            cond.append("entity_type = ?")
            args.append(entity_type)
        if entity_id:
            cond.append("entity_id = ?")
            args.append(entity_id)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY seq"
        rows = self._conn.execute(sql, args).fetchall()
        return [
            {
                "seq": r["seq"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
                "entity_type": r["entity_type"], "entity_id": r["entity_id"],
                "details": json.loads(r["details"]),
                "prev_hash": r["prev_hash"], "hash": r["hash"],
            }
            for r in rows
        ]

    def verify_audit(self) -> dict:
        """重放哈希链，返回是否完整及首处断链位置。"""
        prev = GENESIS
        for entry in self.list_audit():
            payload = canonical({
                "ts": entry["ts"], "actor": entry["actor"], "action": entry["action"],
                "entity_type": entry["entity_type"], "entity_id": entry["entity_id"],
                "details": entry["details"], "prev_hash": entry["prev_hash"],
            })
            expect = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            if entry["prev_hash"] != prev or entry["hash"] != expect:
                return {"ok": False, "broken_at_seq": entry["seq"]}
            prev = entry["hash"]
        return {"ok": True, "entries": len(self.list_audit())}
