"""业务编排：方案提交、复核批准、进场撤场、紧急抢修、暂停恢复与审计。"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .conflicts import Conflict, evaluate_requests, slice_request
from .contracts import TunnelSection
from .models import (
    OCCUPYING_STATUSES,
    PERMIT_ACCEPTANCE,
    PERMIT_ACTIVE,
    PERMIT_APPROVED,
    PERMIT_CANCELLED,
    PERMIT_COMPLETED,
    PERMIT_EXPIRED,
    PERMIT_SUSPENDED,
    PLAN_APPROVED,
    PLAN_EMERGENCY,
    PLAN_NORMAL,
    PLAN_SUBMITTED,
    PLAN_SUPERSEDED,
    PLAN_TECH_APPROVED,
    PLAN_TECH_REJECTED,
    PRIORITY_EMERGENCY,
    PRIORITY_NORMAL,
    OccupancyRequest,
    fmt_dt,
    parse_dt,
)
from .store import Store, canonical


class ServiceError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# 进场后许可的占位延展到撤场验收为止（验收完成前不得释放占位）
FAR_FUTURE = datetime(9999, 12, 31, tzinfo=timezone.utc)
ON_SITE_STATUSES = (PERMIT_ACTIVE, PERMIT_SUSPENDED, PERMIT_ACCEPTANCE)


class GalleryService:
    def __init__(self, db_path: str = ":memory:", clock=None):
        self.store = Store(db_path)
        self._clock = clock or _utcnow

    # ---------------------------------------------------------------- 工具
    def _now(self) -> datetime:
        return self._clock()

    def _actor_name(self, actor: dict) -> str:
        return actor.get("name", "anonymous")

    @staticmethod
    def _require_role(actor: dict, role: str) -> None:
        if role not in actor.get("roles", []):
            raise ServiceError(
                "FORBIDDEN", f"操作需要角色 {role}，当前身份不具备", {"role": role}
            )

    def _audit(self, actor: dict, action: str, entity_type: str, entity_id: str, details: dict) -> None:
        self.store.append_audit(
            fmt_dt(self._now()), self._actor_name(actor), action, entity_type, entity_id, details
        )

    def _idempotent(self, key: str | None, actor: dict, payload: dict, fn):
        """幂等执行：相同键+相同请求返回原结果；相同键+不同请求报错。"""
        if not key:
            return fn()
        request_hash = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()
        existing = self.store.get_idempotency(key)
        if existing:
            if existing["request_hash"] != request_hash:
                raise ServiceError(
                    "IDEMPOTENCY_CONFLICT",
                    f"幂等键 {key} 已用于其他请求，不能复用",
                    {"key": key},
                )
            response = json.loads(existing["response"])
            response["idempotent_replay"] = True
            return response
        result = fn()
        self.store.put_idempotency(
            key, self._actor_name(actor), request_hash, result, fmt_dt(self._now())
        )
        return result

    # ---------------------------------------------------------------- 区段
    def add_section(self, *, actor: dict, section: dict) -> dict:
        doc = TunnelSection.from_dict(section).to_dict()  # 校验
        self.store.put_section(doc)
        self._audit(actor, "SECTION_UPSERTED", "section", doc["section_id"], doc)
        return doc

    def list_sections(self) -> list:
        return self.store.list_sections()

    def _sections(self) -> list:
        return [TunnelSection.from_dict(d) for d in self.store.list_sections()]

    # ---------------------------------------------------------------- 占用视图
    def _refresh_permit(self, permit: dict) -> dict:
        """惰性过期：批准未进场且已过有效期的许可置为 EXPIRED 并释放占位。"""
        if permit["status"] == PERMIT_APPROVED and self._now() > parse_dt(permit["valid_until"]):
            permit["status"] = PERMIT_EXPIRED
            self.store.put_permit(permit)
            self.store.append_audit(
                fmt_dt(self._now()), "system", "PERMIT_EXPIRED", "permit",
                permit["permit_id"], {"valid_until": permit["valid_until"]},
            )
        return permit

    def _occupancy_views(self, exclude_permit_ids=(), include_submitted=False) -> list:
        """当前占用视图：已批准/作业中/已暂停/待验收的许可占位（验收前不释放）。"""
        views = []
        for permit in self.store.list_permits(status_in=OCCUPYING_STATUSES):
            permit = self._refresh_permit(permit)
            if permit["status"] not in OCCUPYING_STATUSES:
                continue
            if permit["permit_id"] in exclude_permit_ids:
                continue
            plan = self.store.get_plan(permit["plan_id"], permit["version"])
            on_site = permit["status"] in ON_SITE_STATUSES
            check_in_at = parse_dt(permit["check_in_at"]) if permit.get("check_in_at") else None
            for req in self._plan_requests(plan):
                placed = slice_request(
                    req, self._sections(),
                    plan_id=plan["plan_id"], version=plan["version"],
                    permit_id=permit["permit_id"],
                )
                if on_site:
                    # 已进场：自进场之时起占位持续有效，直至撤场验收完成
                    start = min(req.time_start, check_in_at) if check_in_at else req.time_start
                    placed = [replace(p, time_start=start, time_end=FAR_FUTURE) for p in placed]
                views.extend(placed)
        if include_submitted:
            for plan in self.store.list_plans(status=PLAN_SUBMITTED):
                for req in self._plan_requests(plan):
                    views.extend(
                        slice_request(
                            req, self._sections(),
                            plan_id=plan["plan_id"], version=plan["version"],
                        )
                    )
        return views

    @staticmethod
    def _plan_requests(plan: dict) -> list:
        return [OccupancyRequest.from_dict(o) for o in plan["occupancies"]]

    def _evaluate(self, requests: list, plan_id: str = "", version: int = 0,
                  exclude_permit_ids=(), include_submitted=False) -> list:
        return evaluate_requests(
            requests,
            self._sections(),
            self._occupancy_views(
                exclude_permit_ids=exclude_permit_ids, include_submitted=include_submitted
            ),
            plan_id=plan_id,
            version=version,
        )

    @staticmethod
    def _blocking(conflicts: list) -> list:
        return [c for c in conflicts if c.blocking]

    # ---------------------------------------------------------------- 方案提交与变更
    def submit_plan(self, *, actor: dict, occupancies: list, depends_on=(),
                    idempotency_key: str | None = None) -> dict:
        payload = {"occupancies": occupancies, "depends_on": list(depends_on)}

        def _do():
            requests = self._parse_requests(occupancies)
            plan_id = self.store.next_id("PL")
            conflicts = self._evaluate(requests, plan_id=plan_id, version=1,
                                       include_submitted=True)
            plan = {
                "plan_id": plan_id,
                "version": 1,
                "kind": PLAN_NORMAL,
                "status": PLAN_SUBMITTED,
                "occupancies": [r.to_dict() for r in requests],
                "depends_on": list(depends_on),
                "submitted_by": self._actor_name(actor),
                "submitted_at": fmt_dt(self._now()),
                "conflicts": [c.to_dict() for c in conflicts],
                "reviews": [],
                "permit_id": None,
            }
            self.store.put_plan(plan)
            self._audit(actor, "PLAN_SUBMITTED", "plan", plan_id, {
                "version": 1,
                "occupancy_count": len(requests),
                "blocking_conflicts": len(self._blocking(conflicts)),
            })
            return self._plan_view(plan, conflicts)

        return self._idempotent(idempotency_key, actor, payload, _do)

    def amend_plan(self, *, actor: dict, plan_id: str, occupancies: list,
                   depends_on=None, idempotency_key: str | None = None) -> dict:
        payload = {"plan_id": plan_id, "occupancies": occupancies, "depends_on": depends_on}

        def _do():
            latest = self._must_plan(plan_id)
            if latest["status"] in (PLAN_SUPERSEDED,):
                raise ServiceError("BAD_STATE", f"方案 {plan_id} 已被新版本取代，不能再次变更")
            permit_id = latest.get("permit_id")
            if permit_id:
                permit = self._refresh_permit(self._must_permit(permit_id))
                if permit["status"] in (PERMIT_ACTIVE, PERMIT_SUSPENDED, PERMIT_ACCEPTANCE):
                    raise ServiceError(
                        "BAD_STATE", "方案已进场作业，不得变更；请先撤场验收", {}
                    )
                if permit["status"] == PERMIT_APPROVED:
                    # 方案变更使旧审批作废，需重新走审批
                    permit["status"] = PERMIT_CANCELLED
                    permit["cancel_reason"] = "方案变更，原审批作废"
                    self.store.put_permit(permit)
                    self._audit(actor, "PERMIT_CANCELLED", "permit", permit_id,
                                {"reason": "方案变更，原审批作废"})
            requests = self._parse_requests(occupancies)
            new_version = latest["version"] + 1
            conflicts = self._evaluate(requests, plan_id=plan_id, version=new_version,
                                       include_submitted=True)
            latest["status"] = PLAN_SUPERSEDED
            self.store.put_plan(latest)
            plan = {
                "plan_id": plan_id,
                "version": new_version,
                "kind": PLAN_NORMAL,
                "status": PLAN_SUBMITTED,
                "occupancies": [r.to_dict() for r in requests],
                "depends_on": list(depends_on) if depends_on is not None else latest["depends_on"],
                "submitted_by": self._actor_name(actor),
                "submitted_at": fmt_dt(self._now()),
                "conflicts": [c.to_dict() for c in conflicts],
                "reviews": [],
                "permit_id": None,
                "supersedes": latest["version"],
            }
            self.store.put_plan(plan)
            self._audit(actor, "PLAN_AMENDED", "plan", plan_id, {
                "version": new_version, "supersedes": latest["version"],
                "blocking_conflicts": len(self._blocking(conflicts)),
            })
            return self._plan_view(plan, conflicts)

        return self._idempotent(idempotency_key, actor, payload, _do)

    # ---------------------------------------------------------------- 审批
    def tech_review(self, *, actor: dict, plan_id: str, version: int,
                    approve: bool, comment: str = "") -> dict:
        self._require_role(actor, "tech_reviewer")
        plan = self._must_plan(plan_id, version)
        self._must_latest(plan)
        if plan["status"] != PLAN_SUBMITTED:
            raise ServiceError("BAD_STATE", f"方案当前状态 {plan['status']}，不能复核")
        if approve:
            blocking = self._blocking(self._evaluate(
                self._plan_requests(plan), plan_id, version, include_submitted=False))
            if blocking:
                raise ServiceError(
                    "CONFLICT", "存在未消除的空间/时间冲突，不得通过技术复核",
                    {"conflicts": [c.to_dict() for c in blocking]},
                )
            plan["status"] = PLAN_TECH_APPROVED
        else:
            plan["status"] = PLAN_TECH_REJECTED
        plan["reviews"].append({
            "stage": "TECH", "result": "APPROVED" if approve else "REJECTED",
            "by": self._actor_name(actor), "at": fmt_dt(self._now()), "comment": comment,
        })
        self.store.put_plan(plan)
        self._audit(actor, "TECH_REVIEWED", "plan", plan_id,
                    {"version": version, "approve": approve, "comment": comment})
        return self._plan_view(plan)

    def ops_approve(self, *, actor: dict, plan_id: str, version: int,
                    valid_hours: float = 24.0) -> dict:
        self._require_role(actor, "ops_approver")
        plan = self._must_plan(plan_id, version)
        self._must_latest(plan)
        if plan["status"] != PLAN_TECH_APPROVED:
            raise ServiceError("BAD_STATE", f"方案当前状态 {plan['status']}，不能运营批准")
        self._check_dependencies(plan)
        blocking = self._blocking(self._evaluate(
            self._plan_requests(plan), plan_id, version, include_submitted=False))
        if blocking:
            raise ServiceError(
                "CONFLICT", "批准前复核发现冲突，不予批准",
                {"conflicts": [c.to_dict() for c in blocking]},
            )
        now = self._now()
        permit = {
            "permit_id": self.store.next_id("PM"),
            "plan_id": plan_id,
            "version": version,
            "kind": PLAN_NORMAL,
            "status": PERMIT_APPROVED,
            "priority": PRIORITY_NORMAL,
            "valid_from": fmt_dt(now),
            "valid_until": fmt_dt(now + timedelta(hours=valid_hours)),
            "approved_by": self._actor_name(actor),
            "approved_at": fmt_dt(now),
            "check_in_at": None,
            "suspended_by": None,
            "suspended_at": None,
            "suspend_reason": None,
            "previous_status": None,
            "acceptance_requested_at": None,
            "completed_at": None,
            "emergency_level": 0,
            "reason": "",
            "recovery": [],
        }
        self.store.put_permit(permit)
        plan["status"] = PLAN_APPROVED
        plan["permit_id"] = permit["permit_id"]
        plan["reviews"].append({
            "stage": "OPS", "result": "APPROVED",
            "by": self._actor_name(actor), "at": fmt_dt(now),
            "comment": f"有效期至 {permit['valid_until']}",
        })
        self.store.put_plan(plan)
        self._audit(actor, "OPS_APPROVED", "permit", permit["permit_id"], {
            "plan_id": plan_id, "version": version, "valid_until": permit["valid_until"],
        })
        return {"plan": self._plan_view(plan), "permit": permit}

    def _check_dependencies(self, plan: dict) -> None:
        unmet = []
        for dep_id in plan.get("depends_on", []):
            dep = self.store.get_plan(dep_id)
            ok = False
            if dep and dep.get("permit_id"):
                dep_permit = self.store.get_permit(dep["permit_id"])
                ok = bool(dep_permit and dep_permit["status"] == PERMIT_COMPLETED)
            if not ok:
                unmet.append(dep_id)
        if unmet:
            raise ServiceError(
                "DEPENDENCY",
                f"前置方案尚未验收完成：{', '.join(unmet)}",
                {"unmet_dependencies": unmet},
            )

    # ---------------------------------------------------------------- 进场 / 撤场 / 验收
    def check_in(self, *, actor: dict, permit_id: str) -> dict:
        permit = self._must_permit(permit_id)
        now = self._now()
        if permit["status"] == PERMIT_APPROVED and now > parse_dt(permit["valid_until"]):
            # 过期审批不能生效：置为 EXPIRED 并拒绝进场
            permit["status"] = PERMIT_EXPIRED
            self.store.put_permit(permit)
            self._audit(actor, "PERMIT_EXPIRED", "permit", permit_id,
                        {"valid_until": permit["valid_until"]})
            raise ServiceError("APPROVAL_EXPIRED", "审批已过有效期，不能进场",
                               {"valid_until": permit["valid_until"]})
        permit = self._refresh_permit(permit)
        if permit["status"] != PERMIT_APPROVED:
            raise ServiceError("BAD_STATE", f"许可当前状态 {permit['status']}，不能进场")
        plan = self.store.get_plan(permit["plan_id"], permit["version"])
        blocking = self._blocking(self._evaluate(
            self._plan_requests(plan), permit["plan_id"], permit["version"],
            exclude_permit_ids=(permit_id,)))
        if blocking:
            raise ServiceError(
                "CONFLICT", "进场前复核发现冲突，禁止进场",
                {"conflicts": [c.to_dict() for c in blocking]},
            )
        permit["status"] = PERMIT_ACTIVE
        permit["check_in_at"] = fmt_dt(now)
        self.store.put_permit(permit)
        self._audit(actor, "CHECKED_IN", "permit", permit_id, {})
        return permit

    def request_acceptance(self, *, actor: dict, permit_id: str) -> dict:
        permit = self._refresh_permit(self._must_permit(permit_id))
        if permit["status"] != PERMIT_ACTIVE:
            raise ServiceError("BAD_STATE", f"许可当前状态 {permit['status']}，不能申请撤场验收")
        permit["status"] = PERMIT_ACCEPTANCE
        permit["acceptance_requested_at"] = fmt_dt(self._now())
        self.store.put_permit(permit)
        self._audit(actor, "ACCEPTANCE_REQUESTED", "permit", permit_id, {})
        return permit

    def complete_acceptance(self, *, actor: dict, permit_id: str,
                            passed: bool, comment: str = "") -> dict:
        self._require_role(actor, "acceptor")
        permit = self._refresh_permit(self._must_permit(permit_id))
        if permit["status"] != PERMIT_ACCEPTANCE:
            raise ServiceError("BAD_STATE", f"许可当前状态 {permit['status']}，不能验收")
        if passed:
            permit["status"] = PERMIT_COMPLETED
            permit["completed_at"] = fmt_dt(self._now())
            self.store.put_permit(permit)
            self._audit(actor, "ACCEPTANCE_PASSED", "permit", permit_id, {"comment": comment})
            if permit["kind"] == PLAN_EMERGENCY:
                self._run_recovery(permit, actor)
        else:
            permit["status"] = PERMIT_ACTIVE
            self.store.put_permit(permit)
            self._audit(actor, "ACCEPTANCE_FAILED", "permit", permit_id, {"comment": comment})
        return self._must_permit(permit_id)

    # ---------------------------------------------------------------- 紧急抢修
    def emergency_occupy(self, *, actor: dict, occupancies: list, reason: str,
                         idempotency_key: str | None = None) -> dict:
        payload = {"occupancies": occupancies, "reason": reason}

        def _do():
            requests = self._parse_requests(occupancies)
            level = int(actor.get("emergency_level", 0))
            sections = self._sections()
            touched = {p.section_id for r in requests for p in slice_request(r, sections)}
            required = max(
                (s.emergency_min_level for s in sections if s.section_id in touched),
                default=1,
            )
            if level < required:
                raise ServiceError(
                    "FORBIDDEN",
                    f"抢修权限不足：涉及区段要求 L{required}，当前 L{level}",
                    {"required_level": required, "actor_level": level},
                )
            now = self._now()
            plan_id = self.store.next_id("PL")
            conflicts = self._evaluate(requests, plan_id=plan_id, version=1)
            plan = {
                "plan_id": plan_id,
                "version": 1,
                "kind": PLAN_EMERGENCY,
                "status": PLAN_APPROVED,
                "occupancies": [r.to_dict() for r in requests],
                "depends_on": [],
                "submitted_by": self._actor_name(actor),
                "submitted_at": fmt_dt(now),
                "conflicts": [c.to_dict() for c in conflicts],
                "reviews": [],
                "permit_id": None,
            }
            permit = {
                "permit_id": self.store.next_id("PM"),
                "plan_id": plan_id,
                "version": 1,
                "kind": PLAN_EMERGENCY,
                "status": PERMIT_ACTIVE,
                "priority": PRIORITY_EMERGENCY,
                "valid_from": fmt_dt(now),
                "valid_until": fmt_dt(max(r.time_end for r in requests)),
                "approved_by": self._actor_name(actor),
                "approved_at": fmt_dt(now),
                "check_in_at": fmt_dt(now),
                "suspended_by": None,
                "suspended_at": None,
                "suspend_reason": None,
                "previous_status": None,
                "acceptance_requested_at": None,
                "completed_at": None,
                "emergency_level": level,
                "reason": reason,
                "recovery": [],
            }
            plan["permit_id"] = permit["permit_id"]
            self.store.put_plan(plan)
            self.store.put_permit(permit)
            self._audit(actor, "EMERGENCY_OCCUPIED", "permit", permit["permit_id"], {
                "plan_id": plan_id, "reason": reason, "level": level,
            })
            suspended = self._suspend_affected(permit, requests, actor)
            permit["recovery"] = self._plan_recovery(suspended)
            self.store.put_permit(permit)
            self._audit(actor, "RECOVERY_PLANNED", "permit", permit["permit_id"], {
                "order": [e["permit_id"] for e in permit["recovery"]],
            })
            return {
                "permit": self._must_permit(permit["permit_id"]),
                "plan": self._plan_view(self.store.get_plan(plan_id)),
                "suspended": suspended,
                "recovery_plan": permit["recovery"],
                "warnings": [c.to_dict() for c in conflicts],
            }

        return self._idempotent(idempotency_key, actor, payload, _do)

    def _suspend_affected(self, emergency_permit: dict, requests: list, actor: dict) -> list:
        """自动暂停与抢修占位冲突的许可（按有效占用窗口判定）。"""
        from .conflicts import check_pair

        sections = self._sections()
        section_by_id = {s.section_id: s for s in sections}
        em_placed = []
        for req in requests:
            em_placed.extend(slice_request(req, sections))
        by_permit: dict = {}
        for view in self._occupancy_views(
                exclude_permit_ids=(emergency_permit["permit_id"],)):
            if view.permit_id:
                by_permit.setdefault(view.permit_id, []).append(view)
        suspended = []
        for permit_id, placed in by_permit.items():
            hits = []
            for a in em_placed:
                for b in placed:
                    if a.section_id != b.section_id:
                        continue
                    hits.extend(check_pair(b, a, section_by_id[a.section_id]))
            blocking_hits = [h for h in hits if h.blocking]
            if not blocking_hits:
                continue
            permit = self._must_permit(permit_id)
            permit["previous_status"] = permit["status"]
            permit["status"] = PERMIT_SUSPENDED
            permit["suspended_by"] = emergency_permit["permit_id"]
            permit["suspended_at"] = fmt_dt(self._now())
            permit["suspend_reason"] = "; ".join(h.message for h in blocking_hits)
            self.store.put_permit(permit)
            self._audit(actor, "PERMIT_SUSPENDED", "permit", permit["permit_id"], {
                "by_emergency": emergency_permit["permit_id"],
                "reasons": [h.message for h in blocking_hits],
            })
            suspended.append({
                "permit_id": permit["permit_id"],
                "previous_status": permit["previous_status"],
                "reasons": [h.message for h in blocking_hits],
            })
        return suspended

    def _plan_recovery(self, suspended: list) -> list:
        """恢复顺序：方案依赖优先，其次先批准先恢复。"""
        permits = [self.store.get_permit(s["permit_id"]) for s in suspended]
        by_plan = {p["plan_id"]: p for p in permits}
        deps = {p["permit_id"]: [] for p in permits}
        for p in permits:
            plan = self.store.get_plan(p["plan_id"], p["version"])
            for dep_plan_id in plan.get("depends_on", []):
                dep = by_plan.get(dep_plan_id)
                if dep:
                    deps[p["permit_id"]].append(dep["permit_id"])
        ordered, done = [], set()
        remaining = list(permits)
        while remaining:
            ready = [p for p in remaining
                     if all(d in done for d in deps[p["permit_id"]])]
            if not ready:  # 依赖成环时退化为按批准时间
                ready = remaining
            ready.sort(key=lambda p: (p["priority"], p["approved_at"], p["permit_id"]))
            pick = ready[0]
            ordered.append(pick)
            done.add(pick["permit_id"])
            remaining.remove(pick)
        return [
            {"permit_id": p["permit_id"], "order": i + 1, "status": "PENDING", "reason": ""}
            for i, p in enumerate(ordered)
        ]

    def _run_recovery(self, emergency_permit: dict, actor: dict) -> None:
        """抢修验收完成后，按恢复顺序逐一恢复被暂停的许可。"""
        em = self._must_permit(emergency_permit["permit_id"])
        changed = False
        for entry in sorted(em["recovery"], key=lambda e: e["order"]):
            permit = self._refresh_permit(self._must_permit(entry["permit_id"]))
            if permit["status"] != PERMIT_SUSPENDED or permit["suspended_by"] != em["permit_id"]:
                entry["status"] = "SKIPPED"
                changed = True
                continue
            plan = self.store.get_plan(permit["plan_id"], permit["version"])
            blocking = self._blocking(self._evaluate_blocking_only(
                plan, exclude_permit_ids=(permit["permit_id"],)))
            if blocking:
                entry["status"] = "PENDING"
                entry["reason"] = "; ".join(c.message for c in blocking)
                self._audit(actor, "RESUME_DEFERRED", "permit", permit["permit_id"], {
                    "reasons": [c.message for c in blocking],
                })
            else:
                permit["status"] = permit["previous_status"] or PERMIT_APPROVED
                permit["suspended_by"] = None
                permit["suspended_at"] = None
                permit["suspend_reason"] = None
                permit["previous_status"] = None
                self.store.put_permit(permit)
                entry["status"] = "RESUMED"
                self._audit(actor, "PERMIT_RESUMED", "permit", permit["permit_id"], {
                    "after_emergency": em["permit_id"], "order": entry["order"],
                })
            changed = True
        if changed:
            self.store.put_permit(em)

    def _evaluate_blocking_only(self, plan: dict, exclude_permit_ids=()) -> list:
        return evaluate_requests(
            self._plan_requests(plan),
            self._sections(),
            self._occupancy_views(exclude_permit_ids=exclude_permit_ids),
            plan_id=plan["plan_id"],
            version=plan["version"],
        )

    # ---------------------------------------------------------------- 查询
    def recovery_plan(self, permit_id: str) -> dict:
        permit = self._must_permit(permit_id)
        if permit["kind"] != PLAN_EMERGENCY:
            raise ServiceError("BAD_STATE", "仅抢修许可有恢复顺序", {})
        return {"permit_id": permit_id, "recovery": permit["recovery"]}

    def get_plan(self, plan_id: str, version: int | None = None) -> dict:
        return self._plan_view(self._must_plan(plan_id, version))

    def plan_conflicts(self, plan_id: str, version: int | None = None) -> dict:
        plan = self._must_plan(plan_id, version)
        conflicts = self._evaluate(
            self._plan_requests(plan), plan["plan_id"], plan["version"],
            include_submitted=True)
        return {"plan_id": plan["plan_id"], "version": plan["version"],
                "conflicts": [c.to_dict() for c in conflicts]}

    def get_permit(self, permit_id: str) -> dict:
        return self._refresh_permit(self._must_permit(permit_id))

    def list_permits(self) -> list:
        return [self._refresh_permit(p) for p in self.store.list_permits()]

    def section_occupancy(self, section_id: str) -> dict:
        if not self.store.get_section(section_id):
            raise ServiceError("NOT_FOUND", f"区段 {section_id} 不存在", {})
        items = []
        for permit in self.store.list_permits(status_in=OCCUPYING_STATUSES):
            permit = self._refresh_permit(permit)
            if permit["status"] not in OCCUPYING_STATUSES:
                continue
            plan = self.store.get_plan(permit["plan_id"], permit["version"])
            for req in self._plan_requests(plan):
                for p in slice_request(req, self._sections(), permit_id=permit["permit_id"]):
                    if p.section_id != section_id:
                        continue
                    items.append({
                        "permit_id": permit["permit_id"],
                        "permit_status": permit["status"],
                        "plan_id": plan["plan_id"],
                        "request_id": req.request_id,
                        "chainage": list(p.envelope.chainage(
                            next(s for s in self._sections() if s.section_id == section_id))),
                        "rect": req.to_dict()["rect"],
                        "utility": req.utility,
                        "process": req.process,
                        "time_start": fmt_dt(req.time_start),
                        "time_end": fmt_dt(req.time_end),
                    })
        return {"section_id": section_id, "occupancies": items}

    def audit_trail(self, entity_type: str | None = None, entity_id: str | None = None) -> list:
        return self.store.list_audit(entity_type, entity_id)

    def verify_audit(self) -> dict:
        return self.store.verify_audit()

    # ---------------------------------------------------------------- 内部
    @staticmethod
    def _parse_requests(occupancies: list) -> list:
        if not occupancies:
            raise ServiceError("VALIDATION", "占位需求不能为空", {})
        try:
            return [OccupancyRequest.from_dict(o) for o in occupancies]
        except (KeyError, ValueError, TypeError) as exc:
            raise ServiceError("VALIDATION", f"占位需求格式错误：{exc}", {})

    def _must_plan(self, plan_id: str, version: int | None = None) -> dict:
        plan = self.store.get_plan(plan_id, version)
        if not plan:
            raise ServiceError("NOT_FOUND", f"方案 {plan_id} 不存在", {"plan_id": plan_id})
        return plan

    def _must_latest(self, plan: dict) -> None:
        latest = self.store.get_plan(plan["plan_id"])
        if latest["version"] != plan["version"]:
            raise ServiceError(
                "STALE_VERSION",
                f"方案已存在更新版本 v{latest['version']}，v{plan['version']} 的审批不能生效",
                {"latest_version": latest["version"]},
            )

    def _must_permit(self, permit_id: str) -> dict:
        permit = self.store.get_permit(permit_id)
        if not permit:
            raise ServiceError("NOT_FOUND", f"许可 {permit_id} 不存在", {"permit_id": permit_id})
        return permit

    def _plan_view(self, plan: dict, conflicts: list | None = None) -> dict:
        view = dict(plan)
        if conflicts is not None:
            view["conflicts"] = [c.to_dict() for c in conflicts]
        view["blocking_conflicts"] = len([
            c for c in view.get("conflicts", []) if c.get("blocking")
        ])
        return view
