"""端到端演示：多家施工方同舱段申请、冲突解释、抢修暂停与恢复、审计验证。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .service import GalleryService

T0 = datetime(2026, 10, 5, 8, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def build_corridor(service: GalleryService, actor: dict) -> None:
    """两段连续区段：SEC-A (K0+000~K0+200) 与 SEC-B (K0+200~K0+400)。"""
    service.add_section(actor=actor, section={
        "section_id": "SEC-A", "start_m": 0, "end_m": 200,
        "width_m": 3.0, "height_m": 2.5, "min_separation_m": 0.3,
        "zones": [
            {"zone_id": "Z-PWR", "name": "既有电力电缆", "utility": "POWER",
             "rect": {"x_m": 0.0, "y_m": 1.8, "width_m": 0.4, "height_m": 0.6},
             "required_clearance_m": 0.5},
            {"zone_id": "Z-TEL", "name": "既有通信桥架", "utility": "TELECOM",
             "rect": {"x_m": 2.6, "y_m": 1.8, "width_m": 0.4, "height_m": 0.6},
             "required_clearance_m": 0.5},
        ],
    })
    service.add_section(actor=actor, section={
        "section_id": "SEC-B", "start_m": 200, "end_m": 400,
        "width_m": 3.0, "height_m": 2.5, "min_separation_m": 0.3,
        "zones": [
            {"zone_id": "Z-WTR", "name": "既有给水管道", "utility": "WATER",
             "rect": {"x_m": 0.0, "y_m": 0.0, "width_m": 0.5, "height_m": 0.5},
             "required_clearance_m": 0.4},
        ],
    })


def contractor_plan(request_id: str, start_m: float, end_m: float,
                    x: float, w: float, utility: str, process: str,
                    hours_from: float, hours_to: float, h: float = 1.8) -> dict:
    return {
        "request_id": request_id,
        "chainage_start_m": start_m,
        "chainage_end_m": end_m,
        "rect": {"x_m": x, "y_m": 0.0, "width_m": w, "height_m": h},
        "utility": utility,
        "process": process,
        "time_start": _iso(T0 + timedelta(hours=hours_from)),
        "time_end": _iso(T0 + timedelta(hours=hours_to)),
    }


def run_demo(service: GalleryService | None = None) -> list:
    """运行完整演示，返回各步骤结果（dict 列表）。"""
    service = service or GalleryService(":memory:")
    ops = {"name": "运营-王工", "roles": ["ops_approver", "acceptor"], "emergency_level": 0}
    tech = {"name": "复核-李工", "roles": ["tech_reviewer"], "emergency_level": 0}
    alpha = {"name": "施工方甲", "roles": [], "emergency_level": 0}
    beta = {"name": "施工方乙", "roles": [], "emergency_level": 0}
    repair = {"name": "抢修队", "roles": [], "emergency_level": 2}
    steps = []

    # 1. 建廊
    build_corridor(service, ops)
    steps.append({"step": "1-建廊", "sections": [s["section_id"] for s in service.list_sections()]})

    # 2. 施工方甲：跨区段方案（SEC-A+SEC-B 连续里程 K0+150~K0+260）
    plan_a = service.submit_plan(
        actor=alpha, idempotency_key="alpha-submit-1",
        occupancies=[contractor_plan("RQ-A1", 150, 260, 1.0, 1.0, "POWER", "CABLE_LAYING", 0, 8)],
    )
    steps.append({"step": "2-甲提交跨区段方案", "plan_id": plan_a["plan_id"],
                  "blocking_conflicts": plan_a["blocking_conflicts"]})

    # 幂等重放：相同键重复提交返回原结果
    replay = service.submit_plan(
        actor=alpha, idempotency_key="alpha-submit-1",
        occupancies=[contractor_plan("RQ-A1", 150, 260, 1.0, 1.0, "POWER", "CABLE_LAYING", 0, 8)],
    )
    steps.append({"step": "2b-幂等重放", "same_plan": replay["plan_id"] == plan_a["plan_id"],
                  "idempotent_replay": replay.get("idempotent_replay", False)})

    # 3. 甲走完审批并进场
    service.tech_review(actor=tech, plan_id=plan_a["plan_id"], version=1, approve=True)
    approved = service.ops_approve(actor=ops, plan_id=plan_a["plan_id"], version=1, valid_hours=24)
    permit_a = approved["permit"]["permit_id"]
    service.check_in(actor=alpha, permit_id=permit_a)
    steps.append({"step": "3-甲批准进场", "permit_id": permit_a, "status": "ACTIVE"})

    # 4. 施工方乙：同舱段同时段申请，净距不足 + 工序倒置 → 可解释冲突
    plan_b = service.submit_plan(
        actor=beta,
        occupancies=[contractor_plan("RQ-B1", 140, 220, 2.05, 0.5, "TELECOM", "RESTORE", 2, 6, h=1.2)],
    )
    steps.append({"step": "4-乙提交冲突方案", "plan_id": plan_b["plan_id"],
                  "conflicts": [c["message"] for c in plan_b["conflicts"] if c["blocking"]]})

    # 5. 紧急抢修：给水舱段爆管，L2 权限临时占用，自动暂停甲
    em = service.emergency_occupy(
        actor=repair, reason="K0+180 给水支管爆裂",
        occupancies=[{
            "request_id": "RQ-E1", "chainage_start_m": 160, "chainage_end_m": 240,
            "rect": {"x_m": 0.4, "y_m": 0.0, "width_m": 1.6, "height_m": 2.0},
            "utility": "WATER", "process": "EMERGENCY",
            "time_start": _iso(T0 + timedelta(hours=3)),
            "time_end": _iso(T0 + timedelta(hours=7)),
        }],
    )
    em_permit = em["permit"]["permit_id"]
    steps.append({"step": "5-抢修临时占用", "permit_id": em_permit,
                  "suspended": [s["permit_id"] for s in em["suspended"]],
                  "recovery_order": [e["permit_id"] for e in em["recovery_plan"]]})

    # 6. 抢修撤场验收 → 按恢复顺序恢复甲
    service.request_acceptance(actor=repair, permit_id=em_permit)
    service.complete_acceptance(actor=ops, permit_id=em_permit, passed=True, comment="抢修验收合格")
    recovery = service.recovery_plan(em_permit)
    steps.append({"step": "6-抢修验收后恢复", "recovery": recovery["recovery"],
                  "permit_a_status": service.get_permit(permit_a)["status"]})

    # 7. 甲撤场验收前，占位不释放：乙同位置再提交仍报冲突
    plan_b2 = service.submit_plan(
        actor=beta,
        occupancies=[contractor_plan("RQ-B2", 150, 260, 1.0, 1.0, "TELECOM", "CABLE_LAYING", 9, 12)],
    )
    steps.append({"step": "7-验收前占位未释放",
                  "blocking_conflicts": plan_b2["blocking_conflicts"]})

    # 8. 甲撤场验收 → 占位释放；乙改到次日再提交 → 通过
    service.request_acceptance(actor=alpha, permit_id=permit_a)
    service.complete_acceptance(actor=ops, permit_id=permit_a, passed=True, comment="撤场验收合格")
    plan_b3 = service.submit_plan(
        actor=beta,
        occupancies=[contractor_plan("RQ-B3", 140, 220, 2.05, 0.5, "TELECOM", "CABLE_LAYING", 26, 30, h=1.2)],
    )
    steps.append({"step": "8-甲验收释放后乙再提交",
                  "permit_a_status": service.get_permit(permit_a)["status"],
                  "blocking_conflicts": plan_b3["blocking_conflicts"]})

    # 9. 审计链验证
    verify = service.verify_audit()
    steps.append({"step": "9-审计链验证", "verify": verify,
                  "audit_entries": len(service.audit_trail())})
    return steps
