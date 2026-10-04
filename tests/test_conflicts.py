import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from helpers import CONTRACTOR, TECH, approve_and_check_in, approve_only, build_service, occ


class ConflictDetectionTests(unittest.TestCase):
    def setUp(self):
        self.svc = build_service()

    def kinds(self, plan):
        return {c["kind"] for c in plan["conflicts"] if c["blocking"]}

    def test_clean_cross_section_plan_passes(self):
        # 跨区段（K0+150~K0+260 跨越 SEC-A/SEC-B 边界）且无冲突
        plan = self.svc.submit_plan(
            actor=CONTRACTOR,
            occupancies=[occ("RQ-1", 150, 260, 1.0, 1.0)],
        )
        self.assertEqual(plan["blocking_conflicts"], 0, plan["conflicts"])

    def test_out_of_bounds_is_reported(self):
        plan = self.svc.submit_plan(
            actor=CONTRACTOR,
            occupancies=[occ("RQ-1", 10, 20, 2.5, 1.0)],  # 右界 3.5m > 净宽 3.0m
        )
        self.assertIn("OUT_OF_BOUNDS", self.kinds(plan))

    def test_clearance_to_fixed_facility(self):
        # 距既有电力电缆（x 0~0.4, y 1.8~2.4, 净空 0.5）水平 0.2m
        plan = self.svc.submit_plan(
            actor=CONTRACTOR,
            occupancies=[occ("RQ-1", 10, 20, 0.6, 1.0, utility="TELECOM")],
        )
        kinds = self.kinds(plan)
        self.assertIn("CLEARANCE", kinds)
        msg = next(c for c in plan["conflicts"] if c["kind"] == "CLEARANCE")
        self.assertIn("净空", msg["message"])
        self.assertEqual(msg["required_m"], 0.5)

    def test_no_section_coverage_gap(self):
        # K0+400 之后没有区段
        plan = self.svc.submit_plan(
            actor=CONTRACTOR,
            occupancies=[occ("RQ-1", 300, 500, 1.0, 1.0)],
        )
        self.assertIn("NO_SECTION", self.kinds(plan))

    def test_separation_and_process_order_against_active_permit(self):
        plan_a = self.svc.submit_plan(
            actor=CONTRACTOR, occupancies=[occ("RQ-A", 150, 260, 1.0, 1.0)])
        self.assertEqual(plan_a["blocking_conflicts"], 0)
        approve_and_check_in(self.svc, plan_a["plan_id"])

        # 乙：净距 0.05m（要求 0.5）且工序倒置（线缆敷设未结束即回填恢复）
        plan_b = self.svc.submit_plan(
            actor=CONTRACTOR,
            occupancies=[occ("RQ-B", 140, 220, 2.05, 0.5, utility="TELECOM",
                             process="RESTORE", h_from=2, h_to=6, height=1.2)],
        )
        kinds = self.kinds(plan_b)
        self.assertIn("SEPARATION", kinds)
        self.assertIn("PROCESS_ORDER", kinds)
        sep = next(c for c in plan_b["conflicts"] if c["kind"] == "SEPARATION")
        self.assertAlmostEqual(sep["actual_m"], 0.05)
        self.assertEqual(sep["required_m"], 0.5)
        self.assertIn("PM-", sep["with_permit_id"])

    def test_time_shifted_plan_has_no_spatial_conflict(self):
        plan_a = self.svc.submit_plan(
            actor=CONTRACTOR, occupancies=[occ("RQ-A", 100, 180, 1.0, 1.0)])
        approve_only(self.svc, plan_a["plan_id"])  # 已批准未进场，占用窗口 0~8 时
        # 同位置但工序衔接正确（敷设结束后才恢复）→ 无冲突
        plan_b = self.svc.submit_plan(
            actor=CONTRACTOR,
            occupancies=[occ("RQ-B", 100, 180, 1.0, 1.0, utility="POWER",
                             process="RESTORE", h_from=9, h_to=12)],
        )
        self.assertEqual(plan_b["blocking_conflicts"], 0, plan_b["conflicts"])

    def test_internal_plan_conflict_detected(self):
        # 同一方案内两条占位自相重叠
        plan = self.svc.submit_plan(
            actor=CONTRACTOR,
            occupancies=[
                occ("RQ-1", 10, 50, 1.0, 1.0),
                occ("RQ-2", 30, 70, 1.2, 1.0),
            ],
        )
        self.assertIn("OVERLAP", self.kinds(plan))

    def test_tech_review_rejected_when_blocking_conflicts(self):
        plan_a = self.svc.submit_plan(
            actor=CONTRACTOR, occupancies=[occ("RQ-A", 100, 180, 1.0, 1.0)])
        approve_and_check_in(self.svc, plan_a["plan_id"])
        plan_b = self.svc.submit_plan(
            actor=CONTRACTOR, occupancies=[occ("RQ-B", 100, 180, 1.0, 1.0)])
        from tunnel_booking.service import ServiceError
        with self.assertRaises(ServiceError) as ctx:
            self.svc.tech_review(actor=TECH, plan_id=plan_b["plan_id"], version=1, approve=True)
        self.assertEqual(ctx.exception.code, "CONFLICT")


if __name__ == "__main__":
    unittest.main()
