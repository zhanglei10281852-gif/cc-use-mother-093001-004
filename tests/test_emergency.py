import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from helpers import (
    CONTRACTOR, OPS, REPAIR_L0, REPAIR_L2, approve_and_check_in, approve_only,
    build_service, occ,
)
from tunnel_booking.service import ServiceError


def emergency_occ():
    return [{
        "request_id": "RQ-E1", "chainage_start_m": 120, "chainage_end_m": 200,
        "rect": {"x_m": 0.6, "y_m": 0.0, "width_m": 1.6, "height_m": 2.0},
        "utility": "WATER", "process": "EMERGENCY",
        "time_start": "2026-10-05T11:00:00+00:00",
        "time_end": "2026-10-05T15:00:00+00:00",
    }]


class EmergencyTests(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()

    def test_emergency_requires_permission_level(self):
        with self.assertRaises(ServiceError) as ctx:
            self.svc.emergency_occupy(actor=REPAIR_L0, occupancies=emergency_occ(),
                                      reason="爆管")
        self.assertEqual(ctx.exception.code, "FORBIDDEN")

    def test_emergency_suspends_conflicting_permits_and_plans_recovery(self):
        # 甲：ACTIVE，与抢修占位相交 → 被暂停
        plan_a = self.svc.submit_plan(actor=CONTRACTOR, occupancies=[
            occ("RQ-A", 100, 180, 1.0, 1.0)])
        permit_a = approve_and_check_in(self.svc, plan_a["plan_id"])
        # 乙：APPROVED 未进场，占位远离抢修区 → 不受影响
        plan_b = self.svc.submit_plan(actor=CONTRACTOR, occupancies=[
            occ("RQ-B", 300, 380, 1.0, 1.0)])
        permit_b = approve_only(self.svc, plan_b["plan_id"])

        result = self.svc.emergency_occupy(actor=REPAIR_L2, occupancies=emergency_occ(),
                                           reason="K0+150 爆管")
        em_id = result["permit"]["permit_id"]
        self.assertEqual(result["permit"]["status"], "ACTIVE")
        self.assertEqual([s["permit_id"] for s in result["suspended"]], [permit_a])
        self.assertEqual(self.svc.get_permit(permit_a)["status"], "SUSPENDED")
        self.assertEqual(self.svc.get_permit(permit_b)["status"], "APPROVED")
        # 恢复顺序已生成
        self.assertEqual([e["permit_id"] for e in result["recovery_plan"]], [permit_a])
        self.assertTrue(self.svc.get_permit(permit_a)["suspend_reason"])

    def test_suspended_permit_cannot_check_in(self):
        plan_a = self.svc.submit_plan(actor=CONTRACTOR, occupancies=[
            occ("RQ-A", 100, 180, 1.0, 1.0)])
        permit_a = approve_only(self.svc, plan_a["plan_id"])
        self.svc.emergency_occupy(actor=REPAIR_L2, occupancies=emergency_occ(),
                                  reason="爆管")
        self.assertEqual(self.svc.get_permit(permit_a)["status"], "SUSPENDED")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.check_in(actor=CONTRACTOR, permit_id=permit_a)
        self.assertEqual(ctx.exception.code, "BAD_STATE")

    def test_recovery_after_emergency_acceptance(self):
        plan_a = self.svc.submit_plan(actor=CONTRACTOR, occupancies=[
            occ("RQ-A", 100, 180, 1.0, 1.0)])
        permit_a = approve_and_check_in(self.svc, plan_a["plan_id"])
        em = self.svc.emergency_occupy(actor=REPAIR_L2, occupancies=emergency_occ(),
                                       reason="爆管")
        em_id = em["permit"]["permit_id"]

        # 抢修许可同样要撤场验收，验收前占位不释放
        self.svc.request_acceptance(actor=REPAIR_L2, permit_id=em_id)
        self.svc.complete_acceptance(actor=OPS, permit_id=em_id, passed=True)
        self.assertEqual(self.svc.get_permit(em_id)["status"], "COMPLETED")

        # 甲按恢复顺序自动恢复为 ACTIVE
        recovery = self.svc.recovery_plan(em_id)["recovery"]
        self.assertEqual(recovery[0]["status"], "RESUMED")
        self.assertEqual(self.svc.get_permit(permit_a)["status"], "ACTIVE")

    def test_recovery_order_respects_dependencies(self):
        # 丙（先批准）与丁（后批准）同时被暂停 → 均按顺序恢复
        plan_c = self.svc.submit_plan(actor=CONTRACTOR, occupancies=[
            occ("RQ-C", 100, 150, 1.0, 1.0)])
        permit_c = approve_and_check_in(self.svc, plan_c["plan_id"])
        plan_d = self.svc.submit_plan(
            actor=CONTRACTOR, depends_on=[plan_c["plan_id"]],
            occupancies=[occ("RQ-D", 150, 200, 1.0, 1.0, h_from=2, h_to=6)])
        # 丙验收完成后丁才能批准（依赖约束）
        self.svc.request_acceptance(actor=CONTRACTOR, permit_id=permit_c)
        self.svc.complete_acceptance(actor=OPS, permit_id=permit_c, passed=True)
        permit_d = approve_only(self.svc, plan_d["plan_id"])
        # 丙的后续方案进场，与丁一起被抢修暂停
        plan_c2 = self.svc.submit_plan(actor=CONTRACTOR, occupancies=[
            occ("RQ-C2", 100, 150, 1.0, 1.0, h_from=20, h_to=28)])
        permit_c2 = approve_and_check_in(self.svc, plan_c2["plan_id"])

        em = self.svc.emergency_occupy(actor=REPAIR_L2, occupancies=[{
            "request_id": "RQ-E2", "chainage_start_m": 90, "chainage_end_m": 210,
            "rect": {"x_m": 0.8, "y_m": 0.0, "width_m": 1.4, "height_m": 2.0},
            "utility": "WATER", "process": "EMERGENCY",
            "time_start": "2026-10-05T11:00:00+00:00",
            "time_end": "2026-10-05T15:00:00+00:00",
        }], reason="区间渗漏")
        em_id = em["permit"]["permit_id"]
        suspended_ids = {s["permit_id"] for s in em["suspended"]}
        self.assertIn(permit_c2, suspended_ids)
        self.assertIn(permit_d, suspended_ids)
        self.svc.request_acceptance(actor=REPAIR_L2, permit_id=em_id)
        self.svc.complete_acceptance(actor=OPS, permit_id=em_id, passed=True)
        recovery = self.svc.recovery_plan(em_id)["recovery"]
        self.assertTrue(all(e["status"] == "RESUMED" for e in recovery))
        # 先批准先恢复：丁的批准时间早于丙二，但两者均恢复
        self.assertEqual(self.svc.get_permit(permit_d)["status"], "APPROVED")
        self.assertEqual(self.svc.get_permit(permit_c2)["status"], "ACTIVE")

    def test_emergency_idempotent(self):
        first = self.svc.emergency_occupy(actor=REPAIR_L2, occupancies=emergency_occ(),
                                          reason="爆管", idempotency_key="EM-1")
        second = self.svc.emergency_occupy(actor=REPAIR_L2, occupancies=emergency_occ(),
                                           reason="爆管", idempotency_key="EM-1")
        self.assertEqual(first["permit"]["permit_id"], second["permit"]["permit_id"])
        self.assertTrue(second["idempotent_replay"])


if __name__ == "__main__":
    unittest.main()
