"""核心服务：空间预约与作业许可全流程。"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from .audit import AuditLog
from .conflicts import detect_dependency_cycles, evaluate_proposal
from .contracts import Proposal
from .errors import (
    ApprovalExpiredError,
    InvalidStateError,
    NotFoundError,
    OccupancyLockedError,
    PermissionDeniedError,
)
from .models import (
    EmergencyRecord,
    HOLDING_STATUSES,
    PermitRequest,
    PermitStatus,
    RequestKind,
    ReviewRecord,
    SuspensionRecord,
    VersionRecord,
)
from .serialization import proposal_to_dict
from .topology import GalleryTopology

DEFAULT_APPROVAL_TTL = timedelta(hours=24)


def _fingerprint(proposal: Proposal) -> str:
    """方案内容指纹：同一 request_id 重复提交相同内容时返回原结果。"""
    payload = json.dumps(proposal_to_dict(proposal), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BookingService:
    """预约与许可服务。

    职责：
    - 方案版本提交与空间/时间/工序冲突计算；
    - 技术复核 + 运营批准两级审批，审批带有效期，过期不得进场；
    - 幂等提交（相同 request_id + 相同方案体返回原版本结果）；
    - 紧急抢修按权限临时占用，自动暂停受影响许可并给出恢复顺序；
    - 撤场验收通过前不释放占位。
    """

    def __init__(
        self,
        topology: GalleryTopology,
        clock: Optional[Callable[[], datetime]] = None,
        approval_ttl: timedelta = DEFAULT_APPROVAL_TTL,
        audit: Optional[AuditLog] = None,
    ) -> None:
        self.topology = topology
        self._clock = clock or datetime.now
        self.approval_ttl = approval_ttl
        self.audit = audit or AuditLog(clock=self._clock)
        self._requests: Dict[str, PermitRequest] = {}
        self._emergencies: Dict[str, EmergencyRecord] = {}
        # (request_id, fingerprint) -> 版本号，用于幂等命中。
        self._idempotency: Dict[Tuple[str, str], int] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 工具

    def now(self) -> datetime:
        return self._clock()

    def _get_request(self, request_id: str) -> PermitRequest:
        req = self._requests.get(request_id)
        if req is None:
            raise NotFoundError(f"申请 {request_id} 不存在")
        return req

    def _holding_proposals(self) -> Dict[str, Proposal]:
        """当前仍占位/排期的各申请当前版本方案。"""
        holders: Dict[str, Proposal] = {}
        for rid, req in self._requests.items():
            v = req.version
            if v.status in HOLDING_STATUSES:
                holders[rid] = v.proposal
        return holders

    # --------------------------------------------------------------- 方案提交

    def submit(
        self,
        proposal: Proposal,
        actor: str,
        base_version: Optional[int] = None,
    ) -> dict:
        """提交方案（首版或变更版）。返回含冲突清单与版本号的结果。

        - 有冲突：版本落 REJECTED，必须修改后提交新版本；
        - 无冲突：版本落 SUBMITTED，等待技术复核；
        - 同一 request_id + 完全相同的方案体：返回原版本结果（幂等）。
        """
        with self._lock:
            fp = _fingerprint(proposal)
            existing_vno = self._idempotency.get((proposal.request_id, fp))
            req = self._requests.get(proposal.request_id)

            if existing_vno is not None and req is not None:
                v = req.versions[existing_vno]
                self.audit.append(
                    actor, "PROPOSAL_RESUBMIT_IDEMPOTENT", "REQUEST",
                    proposal.request_id, "SUCCESS",
                    {"version_no": existing_vno, "status": v.status.value, "idempotent": True},
                )
                return self._version_result(v, idempotent=True)

            if req is None:
                if base_version is not None:
                    raise NotFoundError(
                        f"申请 {proposal.request_id} 不存在，无法基于版本 {base_version} 变更"
                    )
                req = PermitRequest(
                    request_id=proposal.request_id,
                    contractor=proposal.contractor,
                    kind=RequestKind.NORMAL,
                    created_at=self.now(),
                )
                next_no = 1
                expected_base = None
            else:
                latest = req.version
                # 已进场/暂停/验收中的占位版本必须先撤场，不允许直接替换方案。
                if latest.status in (PermitStatus.ACTIVE, PermitStatus.SUSPENDED,
                                     PermitStatus.INSPECTION):
                    raise InvalidStateError(
                        f"申请 {proposal.request_id} 当前版本处于 {latest.status.value}，"
                        "须完成撤场验收后才能提交变更版本"
                    )
                if base_version is None:
                    raise InvalidStateError(
                        f"申请 {proposal.request_id} 已存在，方案变更必须显式基于当前版本 "
                        f"{req.current_version} 提交"
                    )
                if base_version != req.current_version:
                    raise InvalidStateError(
                        f"变更基版本 {base_version} 不是当前版本 {req.current_version}，"
                        "请基于最新版本提交"
                    )
                next_no = req.current_version + 1
                expected_base = base_version

            conflicts = evaluate_proposal(
                proposal,
                self._holding_proposals(),
                self.topology,
                known=self._all_proposals(),
            )
            # 依赖环需要全图视角：在全部方案图上检测，只取本申请相关条目。
            for cycle_conflict in detect_dependency_cycles(self._all_proposals()).get(
                proposal.request_id, []
            ):
                conflicts.append(cycle_conflict)
            status = PermitStatus.SUBMITTED if not conflicts else PermitStatus.REJECTED
            record = VersionRecord(
                version_no=next_no,
                proposal=proposal,
                conflicts=conflicts,
                status=status,
                submitted_at=self.now(),
                submitted_by=actor,
                base_version=expected_base,
            )
            if req.current_version == 0 and not req.versions:
                # 新申请：冲突评估完成后再入库，避免占位集合看到空版本。
                self._requests[proposal.request_id] = req
            req.add_version(record)
            self._idempotency[(proposal.request_id, fp)] = next_no

            self.audit.append(
                actor, "PROPOSAL_SUBMIT", "REQUEST", proposal.request_id,
                "SUCCESS",
                {"version_no": next_no, "base_version": expected_base,
                 "conflict_count": len(conflicts), "status": status.value},
            )
            return self._version_result(record)

    def _all_proposals(self) -> Dict[str, Proposal]:
        return {rid: req.version.proposal
                for rid, req in self._requests.items() if req.versions}

    def _version_result(self, v: VersionRecord, idempotent: bool = False) -> dict:
        return {
            "request_id": v.proposal.request_id,
            "version_no": v.version_no,
            "base_version": v.base_version,
            "status": v.status.value,
            "accepted": not v.conflicts,
            "idempotent": idempotent,
            "conflicts": [c.to_dict() for c in v.conflicts],
        }

    # ------------------------------------------------------------------- 审批

    def _ready_for_review(self, req: PermitRequest, stage: str) -> VersionRecord:
        v = req.version
        if stage == "technical" and v.status != PermitStatus.SUBMITTED:
            raise InvalidStateError(
                f"申请 {req.request_id} 当前状态 {v.status.value}，不能进行技术复核"
            )
        if stage == "operational" and v.status != PermitStatus.TECH_APPROVED:
            raise InvalidStateError(
                f"申请 {req.request_id} 当前状态 {v.status.value}，不能进行运营批准"
            )
        return v

    def technical_review(self, request_id: str, reviewer: str, approved: bool,
                         comment: str = "") -> dict:
        with self._lock:
            req = self._get_request(request_id)
            v = self._ready_for_review(req, "technical")
            rec = ReviewRecord(by=reviewer, at=self.now(), approved=approved, comment=comment)
            v.technical = rec
            if approved:
                v.status = PermitStatus.TECH_APPROVED
                result, action = "SUCCESS", "TECH_REVIEW_APPROVE"
            else:
                v.status = PermitStatus.REJECTED
                result, action = "SUCCESS", "TECH_REVIEW_REJECT"
            self.audit.append(reviewer, action, "REQUEST", request_id, result,
                              {"version_no": v.version_no, "comment": comment})
            return self._version_result(v)

    def operational_approval(self, request_id: str, approver: str, approved: bool,
                             comment: str = "", ttl: Optional[timedelta] = None) -> dict:
        with self._lock:
            req = self._get_request(request_id)
            v = self._ready_for_review(req, "operational")
            effective_ttl = self.approval_ttl if ttl is None else ttl
            expires_at = self.now() + effective_ttl if approved else None
            rec = ReviewRecord(by=approver, at=self.now(), approved=approved,
                               comment=comment, expires_at=expires_at)
            v.operational = rec
            if approved:
                v.status = PermitStatus.APPROVED
                action = "OP_APPROVE"
            else:
                v.status = PermitStatus.REJECTED
                action = "OP_REJECT"
            self.audit.append(approver, action, "REQUEST", request_id, "SUCCESS",
                              {"version_no": v.version_no, "comment": comment,
                               "expires_at": expires_at.isoformat() if expires_at else None})
            return self._version_result(v)

    # ------------------------------------------------------------------- 进场

    def activate(self, request_id: str, actor: str) -> dict:
        """持审批进场；审批超过有效期则拒绝（过期审批不能生效）。"""
        with self._lock:
            req = self._get_request(request_id)
            v = req.version
            if v.status == PermitStatus.EXPIRED:
                raise ApprovalExpiredError(
                    f"申请 {request_id} 的审批已过期，不能进场，须重新提交审批"
                )
            if v.status != PermitStatus.APPROVED:
                raise InvalidStateError(
                    f"申请 {request_id} 当前状态 {v.status.value}，不能进场"
                )
            if not v.approval_valid(self.now()):
                v.status = PermitStatus.EXPIRED
                self.audit.append(actor, "PERMIT_EXPIRE", "REQUEST", request_id, "SUCCESS",
                                  {"version_no": v.version_no,
                                   "expires_at": v.operational.expires_at.isoformat()})
                raise ApprovalExpiredError(
                    f"申请 {request_id} 的运营批准已于 {v.operational.expires_at} 过期，"
                    "不能进场，须重新提交审批"
                )
            v.status = PermitStatus.ACTIVE
            v.activated_at = self.now()
            self.audit.append(actor, "PERMIT_ACTIVATE", "REQUEST", request_id, "SUCCESS",
                              {"version_no": v.version_no})
            return self._version_result(v)

    # --------------------------------------------------------------- 紧急抢修

    def emergency_occupy(
        self,
        proposal: Proposal,
        emergency_id: str,
        declarer: str,
        reason: str = "",
        authorized: bool = True,
    ) -> dict:
        """紧急抢修按权限临时占用。

        - authorized=False（无抢修权限）-> PERMISSION_DENIED；
        - 拓扑净空错误仍会拒绝（不能占用不存在的舱室/压安全通道）；
        - 与在位许可的空间时间冲突不再阻塞，而是自动暂停受影响许可；
        - 计算并返回恢复顺序（优先级 + 依赖拓扑）。
        """
        with self._lock:
            if not authorized:
                self.audit.append(declarer, "EMERGENCY_DENY", "EMERGENCY", emergency_id,
                                  "DENIED", {"reason": reason, "request_id": proposal.request_id})
                raise PermissionDeniedError(
                    f"{declarer} 无紧急抢修占用权限，抢修申请 {emergency_id} 被拒绝"
                )
            if emergency_id in self._emergencies:
                raise InvalidStateError(f"抢修事件 {emergency_id} 已存在")

            holders = self._holding_proposals()
            topo_conflicts = [
                c for c in evaluate_proposal(proposal, {}, self.topology,
                                             known=self._all_proposals())
                if c.category in ("TOPOLOGY", "DEPENDENCY")
            ]
            hard = [c for c in topo_conflicts if c.category == "TOPOLOGY"]
            if hard:
                self.audit.append(declarer, "EMERGENCY_REJECT", "EMERGENCY", emergency_id,
                                  "DENIED", {"conflicts": [c.to_dict() for c in hard]})
                return {"emergency_id": emergency_id, "request_id": proposal.request_id,
                        "accepted": False,
                        "conflicts": [c.to_dict() for c in hard]}

            # 找出受影响的在位许可（时间+空间重叠），仅暂停 ACTIVE 的作业。
            from .conflicts import detect_pair_conflicts
            from .topology import DEFAULT_SEPARATION_M

            affected: List[str] = []
            for rid, other in holders.items():
                if rid == proposal.request_id:
                    continue
                other_req = self._requests[rid]
                if other_req.version.status != PermitStatus.ACTIVE:
                    continue
                if detect_pair_conflicts(proposal, other, DEFAULT_SEPARATION_M):
                    affected.append(rid)

            req = self._requests.get(proposal.request_id)
            if req is None:
                req = PermitRequest(
                    request_id=proposal.request_id,
                    contractor=proposal.contractor,
                    kind=RequestKind.EMERGENCY,
                    created_at=self.now(),
                )
                self._requests[proposal.request_id] = req
            else:
                req.kind = RequestKind.EMERGENCY

            conflicts = evaluate_proposal(proposal, holders, self.topology,
                                          known=self._all_proposals())
            record = VersionRecord(
                version_no=req.current_version + 1,
                proposal=proposal,
                conflicts=conflicts,
                status=PermitStatus.ACTIVE,
                submitted_at=self.now(),
                submitted_by=declarer,
                base_version=req.current_version or None,
                activated_at=self.now(),
            )
            req.add_version(record)

            suspended: List[str] = []
            for rid in affected:
                other_v = self._requests[rid].version
                other_v.status = PermitStatus.SUSPENDED
                other_v.suspension = SuspensionRecord(
                    emergency_id=emergency_id, suspended_at=self.now(), reason=reason
                )
                suspended.append(rid)
                self.audit.append(declarer, "PERMIT_AUTO_SUSPEND", "REQUEST", rid, "SUCCESS",
                                  {"emergency_id": emergency_id, "reason": reason})

            recovery_order = self._compute_recovery_order(suspended)
            em = EmergencyRecord(
                emergency_id=emergency_id,
                request_id=proposal.request_id,
                declared_by=declarer,
                declared_at=self.now(),
                affected_request_ids=suspended,
                recovery_order=recovery_order,
                reason=reason,
            )
            self._emergencies[emergency_id] = em

            self.audit.append(declarer, "EMERGENCY_OCCUPY", "EMERGENCY", emergency_id, "SUCCESS",
                              {"request_id": proposal.request_id, "suspended": suspended,
                               "recovery_order": recovery_order, "reason": reason})
            return {
                "emergency_id": emergency_id,
                "request_id": proposal.request_id,
                "accepted": True,
                "status": PermitStatus.ACTIVE.value,
                "suspended_permits": suspended,
                "recovery_order": recovery_order,
                "conflicts": [c.to_dict() for c in conflicts],
            }

    def _compute_recovery_order(self, request_ids: List[str]) -> List[str]:
        """恢复顺序：先按工序依赖做拓扑序，再按专业优先级（消防/给水优先）。"""
        id_set = set(request_ids)
        selected: Dict[str, Proposal] = {
            rid: self._requests[rid].version.proposal for rid in request_ids
        }

        ordered: List[str] = []
        done = set()

        def priority(rid: str) -> int:
            return selected[rid].effective_priority()

        # Kahn：每轮在“依赖已满足”的集合中取优先级最高者；依赖指向集合外则忽略。
        while len(done) < len(selected):
            ready = [
                rid for rid in selected
                if rid not in done
                and all(d in done or d not in id_set for d in selected[rid].depends_on)
            ]
            if not ready:  # 环兜底：按优先级强行推进
                ready = [rid for rid in selected if rid not in done]
            ready.sort(key=lambda rid: (priority(rid), rid))
            pick = ready[0]
            ordered.append(pick)
            done.add(pick)
        return ordered

    def recover_next(self, emergency_id: str, actor: str) -> dict:
        """抢修结束后按恢复顺序逐个恢复被暂停的许可。"""
        with self._lock:
            em = self._emergencies.get(emergency_id)
            if em is None:
                raise NotFoundError(f"抢修事件 {emergency_id} 不存在")
            pending = [rid for rid in em.recovery_order if rid not in em.restored]
            if not pending:
                raise InvalidStateError(f"抢修事件 {emergency_id} 已无待恢复许可")
            rid = pending[0]
            v = self._requests[rid].version
            if v.status != PermitStatus.SUSPENDED:
                raise InvalidStateError(
                    f"许可 {rid} 当前状态 {v.status.value}，不是暂停态，无法恢复"
                )
            v.status = PermitStatus.ACTIVE
            v.suspension = None
            em.restored.append(rid)
            self.audit.append(actor, "PERMIT_RECOVER", "REQUEST", rid, "SUCCESS",
                              {"emergency_id": emergency_id,
                               "recovery_index": len(em.restored)})
            return {
                "emergency_id": emergency_id,
                "restored_request_id": rid,
                "restored_order": len(em.restored),
                "remaining": pending[1:],
                "status": v.status.value,
            }

    def close_emergency(self, emergency_id: str, actor: str) -> dict:
        with self._lock:
            em = self._emergencies.get(emergency_id)
            if em is None:
                raise NotFoundError(f"抢修事件 {emergency_id} 不存在")
            if not em.is_active:
                raise InvalidStateError(f"抢修事件 {emergency_id} 已关闭")
            ev = self._requests[em.request_id].version
            if ev.status == PermitStatus.ACTIVE:
                ev.status = PermitStatus.INSPECTION
            em.closed_at = self.now()
            self.audit.append(actor, "EMERGENCY_CLOSE", "EMERGENCY", emergency_id, "SUCCESS",
                              {"restored": list(em.restored),
                               "not_restored": [r for r in em.recovery_order
                                                if r not in em.restored]})
            return {"emergency_id": emergency_id, "closed_at": em.closed_at.isoformat(),
                    "restored": list(em.restored), "recovery_order": em.recovery_order}

    # ------------------------------------------------------------------- 撤场

    def request_inspection(self, request_id: str, actor: str) -> dict:
        """申请撤场：进入验收中，占位仍未释放。"""
        with self._lock:
            req = self._get_request(request_id)
            v = req.version
            if v.status != PermitStatus.ACTIVE:
                raise InvalidStateError(
                    f"申请 {request_id} 当前状态 {v.status.value}，不能申请撤场验收"
                )
            v.status = PermitStatus.INSPECTION
            self.audit.append(actor, "INSPECTION_REQUEST", "REQUEST", request_id, "SUCCESS",
                              {"version_no": v.version_no})
            return self._version_result(v)

    def complete_inspection(self, request_id: str, inspector: str, accepted: bool,
                            comment: str = "") -> dict:
        """验收通过才释放占位（COMPLETED）；不通过退回 ACTIVE 继续整改，占位不释放。"""
        with self._lock:
            req = self._get_request(request_id)
            v = req.version
            if v.status != PermitStatus.INSPECTION:
                raise InvalidStateError(
                    f"申请 {request_id} 当前状态 {v.status.value}，不在撤场验收中"
                )
            rec = ReviewRecord(by=inspector, at=self.now(), approved=accepted, comment=comment)
            v.inspection = rec
            if accepted:
                v.status = PermitStatus.COMPLETED
                action, result_status = "INSPECTION_PASS", v.status.value
            else:
                v.status = PermitStatus.ACTIVE
                action, result_status = "INSPECTION_REJECT", v.status.value
            self.audit.append(inspector, action, "REQUEST", request_id, "SUCCESS",
                              {"version_no": v.version_no, "comment": comment,
                               "status": result_status})
            return self._version_result(v)

    def withdraw(self, request_id: str, actor: str) -> dict:
        """进场前撤回；进场后必须走撤场验收，不能直接释放占位。"""
        with self._lock:
            req = self._get_request(request_id)
            v = req.version
            if v.status in (PermitStatus.ACTIVE, PermitStatus.SUSPENDED,
                            PermitStatus.INSPECTION):
                raise OccupancyLockedError(
                    f"申请 {request_id} 已进场（{v.status.value}），撤场验收完成前不得释放占位"
                )
            if v.status in (PermitStatus.COMPLETED, PermitStatus.WITHDRAWN, PermitStatus.EXPIRED,
                            PermitStatus.REJECTED):
                raise InvalidStateError(f"申请 {request_id} 已处于终结状态 {v.status.value}")
            v.status = PermitStatus.WITHDRAWN
            self.audit.append(actor, "PERMIT_WITHDRAW", "REQUEST", request_id, "SUCCESS",
                              {"version_no": v.version_no})
            return self._version_result(v)

    # ------------------------------------------------------------------- 查询

    def get_request(self, request_id: str) -> dict:
        with self._lock:
            req = self._get_request(request_id)
            return self._request_view(req)

    def get_emergency(self, emergency_id: str) -> dict:
        with self._lock:
            em = self._emergencies.get(emergency_id)
            if em is None:
                raise NotFoundError(f"抢修事件 {emergency_id} 不存在")
            return self._emergency_view(em)

    def list_requests(self) -> List[dict]:
        with self._lock:
            return [self._request_view(req) for req in self._requests.values()]

    def status_of(self, request_id: str) -> PermitStatus:
        return self._get_request(request_id).version.status

    def _request_view(self, req: PermitRequest) -> dict:
        v = req.version
        return {
            "request_id": req.request_id,
            "contractor": req.contractor,
            "kind": req.kind.value,
            "current_version": req.current_version,
            "status": v.status.value,
            "holding_occupancy": v.status in HOLDING_STATUSES,
            "version": self._version_result(v),
        }

    def _emergency_view(self, em: EmergencyRecord) -> dict:
        return {
            "emergency_id": em.emergency_id,
            "request_id": em.request_id,
            "declared_by": em.declared_by,
            "declared_at": em.declared_at.isoformat(),
            "reason": em.reason,
            "active": em.is_active,
            "closed_at": em.closed_at.isoformat() if em.closed_at else None,
            "suspended_permits": list(em.affected_request_ids),
            "recovery_order": list(em.recovery_order),
            "restored": list(em.restored),
        }

    # ------------------------------------------------------------- 持久化快照

    def snapshot(self) -> dict:
        """导出可重建状态的快照（用于测试/落盘）。"""
        with self._lock:
            return {
                "topology": self.topology.to_dict(),
                "requests": [self._request_view(req) for req in self._requests.values()],
                "emergencies": [self._emergency_view(em) for em in self._emergencies.values()],
                "audit": self.audit.to_list(),
            }
