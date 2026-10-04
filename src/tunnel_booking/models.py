"""许可聚合：申请、版本、审批记录与状态机。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from .contracts import Proposal
from .conflicts import Conflict


class PermitStatus(str, Enum):
    SUBMITTED = "SUBMITTED"            # 已提交待技术复核
    REJECTED = "REJECTED"              # 复核/批准驳回（该版本终结，可提交新版本）
    TECH_APPROVED = "TECH_APPROVED"    # 技术复核通过，待运营批准
    APPROVED = "APPROVED"              # 两级审批通过，可进场
    ACTIVE = "ACTIVE"                  # 已进场，占位生效
    SUSPENDED = "SUSPENDED"            # 被抢修暂停
    INSPECTION = "INSPECTION"          # 已申请撤场，待验收
    COMPLETED = "COMPLETED"            # 验收通过，占位释放（终结）
    WITHDRAWN = "WITHDRAWN"            # 进场前撤回（终结）
    EXPIRED = "EXPIRED"                # 审批过期未进场（终结）


# 仍持有空间占位/排期权的状态：冲突计算需要把它们视为在位方。
HOLDING_STATUSES = frozenset(
    {
        PermitStatus.SUBMITTED,
        PermitStatus.TECH_APPROVED,
        PermitStatus.APPROVED,
        PermitStatus.ACTIVE,
        PermitStatus.SUSPENDED,
        PermitStatus.INSPECTION,
    }
)

TERMINAL_STATUSES = frozenset(
    {
        PermitStatus.REJECTED,
        PermitStatus.COMPLETED,
        PermitStatus.WITHDRAWN,
        PermitStatus.EXPIRED,
    }
)


class RequestKind(str, Enum):
    NORMAL = "NORMAL"
    EMERGENCY = "EMERGENCY"


@dataclass
class ReviewRecord:
    by: str
    at: datetime
    approved: bool
    comment: str = ""
    expires_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "by": self.by,
            "at": self.at.isoformat(),
            "approved": self.approved,
            "comment": self.comment,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewRecord":
        return cls(
            by=d["by"],
            at=datetime.fromisoformat(d["at"]),
            approved=d["approved"],
            comment=d.get("comment", ""),
            expires_at=datetime.fromisoformat(d["expires_at"]) if d.get("expires_at") else None,
        )


@dataclass
class SuspensionRecord:
    emergency_id: str
    suspended_at: datetime
    reason: str

    def to_dict(self) -> dict:
        return {
            "emergency_id": self.emergency_id,
            "suspended_at": self.suspended_at.isoformat(),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SuspensionRecord":
        return cls(
            emergency_id=d["emergency_id"],
            suspended_at=datetime.fromisoformat(d["suspended_at"]),
            reason=d.get("reason", ""),
        )


@dataclass
class VersionRecord:
    version_no: int
    proposal: Proposal
    conflicts: List[Conflict]
    status: PermitStatus
    submitted_at: datetime
    submitted_by: str
    base_version: Optional[int] = None
    technical: Optional[ReviewRecord] = None
    operational: Optional[ReviewRecord] = None
    activated_at: Optional[datetime] = None
    suspension: Optional[SuspensionRecord] = None
    inspection: Optional[ReviewRecord] = None

    @property
    def is_accepted_submission(self) -> bool:
        return not self.conflicts

    def approval_valid(self, now: datetime) -> bool:
        if self.status != PermitStatus.APPROVED or self.operational is None:
            return False
        if self.operational.expires_at is not None and now > self.operational.expires_at:
            return False
        return True


@dataclass
class PermitRequest:
    request_id: str
    contractor: str
    kind: RequestKind
    created_at: datetime
    versions: Dict[int, VersionRecord] = field(default_factory=dict)
    current_version: int = 0

    @property
    def version(self) -> VersionRecord:
        return self.versions[self.current_version]

    @property
    def status(self) -> PermitStatus:
        return self.version.status

    def add_version(self, record: VersionRecord) -> None:
        self.versions[record.version_no] = record
        self.current_version = record.version_no


@dataclass
class EmergencyRecord:
    emergency_id: str
    request_id: str
    declared_by: str
    declared_at: datetime
    affected_request_ids: List[str]
    recovery_order: List[str]
    reason: str = ""
    closed_at: Optional[datetime] = None
    restored: List[str] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.closed_at is None
