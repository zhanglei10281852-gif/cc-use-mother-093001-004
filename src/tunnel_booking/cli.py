"""命令行场景验证。

用法：
  python run_cli.py demo      运行内置端到端验证场景（跨区段预约/抢修/恢复/审计）
  python run_cli.py serve     启动 HTTP 服务（--host/--port）
  python run_cli.py topology  打印演示管廊拓扑
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .contracts import (
    ChainageInterval,
    Proposal,
    Rect,
    TimeWindow,
    UtilityKind,
    WorkEnvelope,
)
from .demo import build_demo_topology
from .errors import ApprovalExpiredError, BookingError, OccupancyLockedError
from .service import BookingService


# --------------------------------------------------------------- 可变时钟

class MutableClock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


# --------------------------------------------------------------- 构造助手

def env(eid: str, utility: UtilityKind, seg: str, start_m: float, end_m: float,
        x: float, y: float, w: float, h: float) -> WorkEnvelope:
    return WorkEnvelope(
        envelope_id=eid,
        compartment_id="C1",
        segment_id=seg,
        chainage=ChainageInterval(start_m, end_m),
        footprint=Rect(x, y, w, h),
        utility=utility,
    )


def proposal(rid: str, contractor: str, start: str, end: str,
             envelopes: List[WorkEnvelope], depends_on: Tuple[str, ...] = (),
             priority: Optional[int] = None) -> Proposal:
    return Proposal(
        request_id=rid,
        contractor=contractor,
        window=TimeWindow.of(start, end),
        envelopes=tuple(envelopes),
        depends_on=tuple(depends_on),
        priority=priority,
    )


# --------------------------------------------------------------- 断言记录

@dataclass
class Check:
    name: str
    ok: bool
    detail: str


class Scenario:
    def __init__(self, approval_ttl_hours: int = 24) -> None:
        self.clock = MutableClock(datetime(2026, 10, 4, 8, 0))
        self.svc = BookingService(build_demo_topology(), clock=self.clock,
                                  approval_ttl=timedelta(hours=approval_ttl_hours))
        self.checks: List[Check] = []
        self.lines: List[str] = []

    def log(self, text: str) -> None:
        self.lines.append(text)

    def expect(self, name: str, condition: bool, detail: str = "") -> None:
        self.checks.append(Check(name, bool(condition), detail))

    def run(self) -> None:
        s = self.svc
        t = self.clock

        # 1) 跨区段正常预约：电力敷设跨越 S1(0-120) 与 S2(120-240)。
        self.log("== 1. 跨区段预约：电力 P-100 包络跨越 S1/S2 ==")
        p_power = proposal(
            "P-100", "送变电三队", "2026-10-05T08:00", "2026-10-05T18:00",
            [
                env("E-P1", UtilityKind.POWER, "S1", 20, 120, 1.0, 1.0, 0.4, 0.4),
                env("E-P2", UtilityKind.POWER, "S2", 120, 200, 1.0, 1.0, 0.4, 0.4),
            ],
        )
        r = s.submit(p_power, actor="contractor-li")
        self.expect("跨区段方案无冲突", r["accepted"], json.dumps(r["conflicts"], ensure_ascii=False))
        self.expect("版本号 v1", r["version_no"] == 1)
        s.technical_review("P-100", "engineer-wang", True, "截面与里程复核通过")
        s.operational_approval("P-100", "ops-zhao", True, "同意进场")
        s.activate("P-100", "contractor-li")
        self.expect("P-100 进场 ACTIVE", s.status_of("P-100").value == "ACTIVE")

        # 2) 净距冲突：通信想在同一时间、同里程、净距 0.1m 处施工。
        self.log("== 2. 隔离距离冲突：通信 C-200 紧贴电力 ==")
        p_comm_close = proposal(
            "C-200", "通信工程队", "2026-10-05T10:00", "2026-10-05T16:00",
            [env("E-C1", UtilityKind.COMM, "S1", 60, 100, 1.5, 1.0, 0.3, 0.3)],
        )
        r = s.submit(p_comm_close, actor="contractor-chen")
        codes = {c["code"] for c in r["conflicts"]}
        self.expect("识别 SEPARATION_VIOLATION", "SEPARATION_VIOLATION" in codes,
                    json.dumps(r["conflicts"], ensure_ascii=False))
        self.expect("给出可解释原因",
                    any("小于规范要求" in c["message"] for c in r["conflicts"]))
        self.expect("冲突方案落 REJECTED", r["status"] == "REJECTED")

        # 3) 时间错开后同位置可以提交（时间不重叠 -> 无冲突）。
        self.log("== 3. 错峰方案：通信改到次日同时段 ==")
        p_comm_ok = proposal(
            "C-200", "通信工程队", "2026-10-06T08:00", "2026-10-06T18:00",
            [env("E-C1", UtilityKind.COMM, "S1", 60, 100, 1.5, 1.0, 0.3, 0.3)],
        )
        r = s.submit(p_comm_ok, actor="contractor-chen", base_version=1)
        self.expect("错峰后无冲突", r["accepted"], json.dumps(r["conflicts"], ensure_ascii=False))

        # 4) 幂等：完全相同的重复提交返回原结果。
        r2 = s.submit(p_comm_ok, actor="contractor-chen", base_version=2)
        self.expect("重复请求命中幂等返回原版本", r2["idempotent"] and r2["version_no"] == 2)

        # 5) 给水预约作为第二路在场作业（工序：依赖电力 P-100，次日夜间）。
        self.log("== 4. 给水 W-300（声明依赖 P-100）正常审批进场 ==")
        p_water = proposal(
            "W-300", "给水安装班", "2026-10-07T08:00", "2026-10-07T16:00",
            [env("E-W1", UtilityKind.WATER, "S2", 140, 190, 2.0, 0.6, 0.5, 0.5)],
            depends_on=("P-100",),
        )
        r = s.submit(p_water, actor="contractor-sun")
        self.expect("依赖满足（时间在前）无冲突", r["accepted"],
                    json.dumps(r["conflicts"], ensure_ascii=False))
        # 时钟推到 10-07（P-100 的作业窗早已结束，无空间冲突），再走当日审批。
        t.advance(days=2, hours=1)
        s.technical_review("W-300", "engineer-wang", True)
        s.operational_approval("W-300", "ops-zhao", True)
        s.activate("W-300", "contractor-sun")
        self.expect("W-300 进场 ACTIVE", s.status_of("W-300").value == "ACTIVE")

        # 6) 消防也在场（第三路，同 S2 区段、与给水时间重叠但截面分开、净距达标）。
        p_fire = proposal(
            "F-400", "消防安装班", "2026-10-07T09:00", "2026-10-07T15:00",
            [env("E-F1", UtilityKind.FIRE, "S2", 150, 200, 1.0, 1.6, 0.4, 0.4)],
            depends_on=("P-100",),
        )
        r = s.submit(p_fire, actor="contractor-zhou")
        self.expect("消防方案净距达标无冲突", r["accepted"],
                    json.dumps(r["conflicts"], ensure_ascii=False))
        s.technical_review("F-400", "engineer-wang", True)
        s.operational_approval("F-400", "ops-zhao", True)
        s.activate("F-400", "contractor-zhou")
        self.expect("F-400 进场 ACTIVE", s.status_of("F-400").value == "ACTIVE")

        # 7) 紧急抢修：消防管线在 S2 爆裂，抢修包络与给水/消防作业空间时间重叠。
        self.log("== 5. 紧急抢修 EMG-1：自动暂停受影响许可 ==")
        p_repair = proposal(
            "EMG-1-REQ", "应急抢修队", "2026-10-07T10:00", "2026-10-07T12:00",
            [env("E-EMG", UtilityKind.FIRE, "S2", 145, 195, 1.2, 0.8, 1.2, 1.2)],
        )
        r = s.emergency_occupy(p_repair, emergency_id="EMG-1", declarer="duty-qian",
                               reason="消防管接驳口泄漏应急处置", authorized=True)
        self.expect("抢修获允并 ACTIVE", r["accepted"] and r["status"] == "ACTIVE")
        suspended = set(r["suspended_permits"])
        self.expect("W-300/F-400 被自动暂停",
                    {"W-300", "F-400"} <= suspended,
                    f"suspended={suspended}")
        self.expect("P-100 不在影响范围（时间窗已过）", "P-100" not in suspended)
        self.expect("恢复顺序消防先于给水（消防优先）",
                    r["recovery_order"].index("F-400") < r["recovery_order"].index("W-300"),
                    f"recovery_order={r['recovery_order']}")
        self.expect("被暂停许可状态为 SUSPENDED",
                    s.status_of("W-300").value == "SUSPENDED"
                    and s.status_of("F-400").value == "SUSPENDED")

        # 8) 无权限抢修被拒绝。
        try:
            s.emergency_occupy(
                proposal("EMG-X", "外包队", "2026-10-07T10:30", "2026-10-07T11:30",
                         [env("E-X", UtilityKind.WATER, "S1", 0, 20, 1.0, 1.0, 0.4, 0.4)]),
                emergency_id="EMG-X", declarer="outsider", authorized=False,
            )
            self.expect("无权限抢修被拒绝", False, "未抛出权限错误")
        except BookingError as exc:
            self.expect("无权限抢修被拒绝", exc.code == "PERMISSION_DENIED", str(exc))

        # 9) 抢修期间被暂停许可不得自行撤场释放。
        self.log("== 6. 按恢复顺序逐个恢复 ==")
        r1 = s.recover_next("EMG-1", "duty-qian")
        self.expect("第一个恢复 F-400", r1["restored_request_id"] == "F-400")
        self.expect("F-400 恢复为 ACTIVE", s.status_of("F-400").value == "ACTIVE")
        r2 = s.recover_next("EMG-1", "duty-qian")
        self.expect("第二个恢复 W-300", r2["restored_request_id"] == "W-300")
        self.expect("剩余队列为空", r2["remaining"] == [])

        # 10) 关闭抢修事件。
        s.close_emergency("EMG-1", "duty-qian")

        # 11) 撤场验收完成前不得释放占位。
        self.log("== 7. 撤场验收：未验收不得释放占位 ==")
        try:
            s.withdraw("W-300", "contractor-sun")
            self.expect("在场许可不能直接撤回释放", False, "未抛出 OccupancyLocked")
        except OccupancyLockedError as exc:
            self.expect("在场许可不能直接撤回释放", exc.code == "OCCUPANCY_LOCKED")
        s.request_inspection("W-300", "contractor-sun")
        self.expect("验收中仍持有占位",
                    s.get_request("W-300")["holding_occupancy"] is True)
        r = s.complete_inspection("W-300", "inspector-zheng", accepted=False,
                                  comment="现场余料未清，整改后复验")
        self.expect("验收不通过退回 ACTIVE 且占位不释放",
                    r["status"] == "ACTIVE" and s.get_request("W-300")["holding_occupancy"])
        s.request_inspection("W-300", "contractor-sun")
        r = s.complete_inspection("W-300", "inspector-zheng", accepted=True,
                                  comment="清场合格")
        self.expect("验收通过后释放占位",
                    r["status"] == "COMPLETED"
                    and s.get_request("W-300")["holding_occupancy"] is False)

        # 12) 过期审批不能生效。
        self.log("== 8. 审批过期不得进场 ==")
        p_late = proposal(
            "C-201", "通信二队", "2026-10-10T08:00", "2026-10-10T12:00",
            [env("E-L1", UtilityKind.COMM, "S1", 0, 40, 0.8, 1.8, 0.3, 0.3)],
        )
        s.submit(p_late, actor="contractor-lin")
        s.technical_review("C-201", "engineer-wang", True)
        s.operational_approval("C-201", "ops-zhao", True)
        t.advance(hours=25)  # 超过 24 小时有效期
        try:
            s.activate("C-201", "contractor-lin")
            self.expect("过期审批进场被拒", False, "未抛出过期错误")
        except ApprovalExpiredError as exc:
            self.expect("过期审批进场被拒", exc.code == "APPROVAL_EXPIRED")
            self.expect("过期后状态 EXPIRED",
                        s.status_of("C-201").value == "EXPIRED")

        # 13) 变更必须基于版本。
        self.log("== 9. 方案变更基于版本提交 ==")
        try:
            s.submit(
                proposal("P-100", "送变电三队", "2026-10-20T08:00", "2026-10-20T18:00",
                         [env("E-P1", UtilityKind.POWER, "S1", 0, 60, 1.0, 1.0, 0.4, 0.4)]),
                actor="contractor-li")
            self.expect("无基版本变更被拒", False, "未抛出错误")
        except BookingError as exc:
            self.expect("无基版本变更被拒", exc.code == "INVALID_STATE")

        # 14) 拓扑净空：压安全通道被拒。
        r = s.submit(
            proposal("X-900", "违规队", "2026-10-11T08:00", "2026-10-11T10:00",
                     [env("E-X", UtilityKind.WATER, "S1", 0, 20, 0.0, 0.0, 0.8, 1.0)]),
            actor="bad")
        self.expect("占用安全通道被识别",
                    "SAFETY_PASSAGE_BLOCKED" in {c["code"] for c in r["conflicts"]})

        # 15) 完整审计记录。
        self.log("== 10. 完整审计记录 ==")
        events = s.audit.to_list()
        self.expect("审计事件连续编号",
                    [e["seq"] for e in events] == list(range(1, len(events) + 1)))
        actions = {e["action"] for e in events}
        for required in ("PROPOSAL_SUBMIT", "TECH_REVIEW_APPROVE", "OP_APPROVE",
                         "PERMIT_ACTIVATE", "EMERGENCY_OCCUPY", "PERMIT_AUTO_SUSPEND",
                         "PERMIT_RECOVER", "INSPECTION_PASS", "PERMIT_EXPIRE"):
            self.expect(f"审计包含 {required}", required in actions)
        emg_events = s.audit.query(entity_type="EMERGENCY", entity_id="EMG-1")
        self.expect("可按抢修事件追溯审计",
                    {e.action for e in emg_events} >= {"EMERGENCY_OCCUPY", "EMERGENCY_CLOSE"})

    def report(self) -> Tuple[str, int]:
        passed = sum(1 for c in self.checks if c.ok)
        total = len(self.checks)
        out = ["# 管廊空间预约与作业许可 —— 端到端验证场景", ""]
        out.extend(self.lines)
        out.append("")
        out.append(f"## 断言结果：{passed}/{total} 通过")
        for c in self.checks:
            mark = "PASS" if c.ok else "FAIL"
            out.append(f"[{mark}] {c.name}" + (f"  -- {c.detail}" if c.detail and not c.ok else ""))
        out.append("")
        out.append(f"## 审计记录（共 {len(self.svc.audit)} 条，按时间排序）")
        for e in self.svc.audit.to_list():
            out.append(
                f"#{e['seq']:>2} {e['at']} {e['actor']:<15} {e['action']:<26} "
                f"{e['entity_type']}/{e['entity_id']} [{e['result']}]"
            )
        return "\n".join(out), 0 if passed == total else 1


def run_demo() -> Tuple[str, int]:
    sc = Scenario()
    sc.run()
    return sc.report()
