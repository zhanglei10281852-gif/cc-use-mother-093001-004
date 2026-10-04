import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from helpers import (
    CONTRACTOR, OPS, T0, TECH, approve_and_check_in, approve_only, build_service, occ,
)
from tunnel_booking.service import ServiceError


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.svc = build_service(clock=self.clock)

    def submit_clean(self, request_id="RQ-1", start=100, end=180, x=1.0, w=1.0, **kw):
        return self.svc.submit_plan(actor=CONTRACTOR, occupancies=[
            occ(request_id, start, end, x, w, **kw)])

    def test_full_lifecycle_releases_occupancy_only_after_acceptance(self):
        plan = self.submit_clean()
        permit_id = approve_and_check_in(self.svc, plan["plan_id"])
        self.assertEqual(self.svc.get_permit(permit_id)["status"], "ACTIVE")

        # 撤场验收前：时间窗已过，占位仍不释放
        self.clock.advance(hours=20)
        later = self.svc.submit_plan(actor=CONTRACTOR, occupancies=[
            occ("RQ-2", 100, 180, 1.0, 1.0, h_from=21, h_to=23)])
        self.assertGreater(later["blocking_conflicts"], 0)

        # 撤场申请 → 验收通过 → 占位释放
        self.svc.request_acceptance(actor=CONTRACTOR, permit_id=permit_id)
        self.assertEqual(self.svc.get_permit(permit_id)["status"], "ACCEPTANCE_PENDING")
        self.svc.complete_acceptance(actor=OPS, permit_id=permit_id, passed=True)
        self.assertEqual(self.svc.get_permit(permit_id)["status"], "COMPLETED")

        again = self.svc.submit_plan(actor=CONTRACTOR, occupancies=[
            occ("RQ-3", 100, 180, 1.0, 1.0, h_from=21, h_to=23)])
        self.assertEqual(again["blocking_conflicts"], 0, again["conflicts"])

    def test_acceptance_failure_returns_permit_to_active(self):
        plan = self.submit_clean()
        permit_id = approve_and_check_in(self.svc, plan["plan_id"])
        self.svc.request_acceptance(actor=CONTRACTOR, permit_id=permit_id)
        self.svc.complete_acceptance(actor=OPS, permit_id=permit_id, passed=False,
                                     comment="现场未清理")
        self.assertEqual(self.svc.get_permit(permit_id)["status"], "ACTIVE")

    def test_expired_approval_cannot_check_in(self):
        plan = self.submit_clean()
        permit_id = approve_only(self.svc, plan["plan_id"], valid_hours=4)
        self.clock.advance(hours=5)  # 已过有效期
        with self.assertRaises(ServiceError) as ctx:
            self.svc.check_in(actor=CONTRACTOR, permit_id=permit_id)
        self.assertEqual(ctx.exception.code, "APPROVAL_EXPIRED")
        self.assertEqual(self.svc.get_permit(permit_id)["status"], "EXPIRED")

    def test_amend_supersedes_version_and_voids_approval(self):
        plan = self.submit_clean()
        permit_id = approve_only(self.svc, plan["plan_id"])

        amended = self.svc.amend_plan(actor=CONTRACTOR, plan_id=plan["plan_id"],
                                      occupancies=[occ("RQ-1B", 100, 160, 1.0, 1.0)])
        self.assertEqual(amended["version"], 2)
        # 旧许可作废，旧版本审批不能再生效
        self.assertEqual(self.svc.get_permit(permit_id)["status"], "CANCELLED")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.tech_review(actor=TECH, plan_id=plan["plan_id"], version=1, approve=True)
        self.assertEqual(ctx.exception.code, "STALE_VERSION")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.ops_approve(actor=OPS, plan_id=plan["plan_id"], version=1)
        self.assertEqual(ctx.exception.code, "STALE_VERSION")
        # 新版本可重新走审批
        new_permit = approve_only(self.svc, plan["plan_id"], version=2)
        self.assertEqual(self.svc.get_permit(new_permit)["status"], "APPROVED")

    def test_amend_rejected_after_check_in(self):
        plan = self.submit_clean()
        approve_and_check_in(self.svc, plan["plan_id"])
        with self.assertRaises(ServiceError) as ctx:
            self.svc.amend_plan(actor=CONTRACTOR, plan_id=plan["plan_id"],
                                occupancies=[occ("RQ-1B", 100, 160, 1.0, 1.0)])
        self.assertEqual(ctx.exception.code, "BAD_STATE")

    def test_idempotent_submit_returns_original(self):
        payload = [occ("RQ-1", 100, 180, 1.0, 1.0)]
        first = self.svc.submit_plan(actor=CONTRACTOR, occupancies=payload,
                                     idempotency_key="K-1")
        second = self.svc.submit_plan(actor=CONTRACTOR, occupancies=payload,
                                      idempotency_key="K-1")
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertTrue(second["idempotent_replay"])
        # 重复请求不产生重复审计
        submits = [a for a in self.svc.audit_trail() if a["action"] == "PLAN_SUBMITTED"]
        self.assertEqual(len(submits), 1)

    def test_idempotency_key_reuse_with_different_payload_fails(self):
        self.svc.submit_plan(actor=CONTRACTOR, occupancies=[occ("RQ-1", 100, 180, 1.0, 1.0)],
                             idempotency_key="K-2")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.submit_plan(actor=CONTRACTOR,
                                 occupancies=[occ("RQ-9", 200, 280, 1.0, 1.0)],
                                 idempotency_key="K-2")
        self.assertEqual(ctx.exception.code, "IDEMPOTENCY_CONFLICT")

    def test_dependency_blocks_ops_approval_until_completed(self):
        plan_a = self.submit_clean("RQ-A", 100, 180)
        plan_b = self.svc.submit_plan(
            actor=CONTRACTOR, depends_on=[plan_a["plan_id"]],
            occupancies=[occ("RQ-B", 100, 180, 1.0, 1.0, h_from=9, h_to=12)])
        self.svc.tech_review(actor=TECH, plan_id=plan_b["plan_id"], version=1, approve=True)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.ops_approve(actor=OPS, plan_id=plan_b["plan_id"], version=1)
        self.assertEqual(ctx.exception.code, "DEPENDENCY")

        # 前置方案验收完成后方可批准
        permit_a = approve_and_check_in(self.svc, plan_a["plan_id"])
        self.svc.request_acceptance(actor=CONTRACTOR, permit_id=permit_a)
        self.svc.complete_acceptance(actor=OPS, permit_id=permit_a, passed=True)
        result = self.svc.ops_approve(actor=OPS, plan_id=plan_b["plan_id"], version=1)
        self.assertEqual(result["permit"]["status"], "APPROVED")

    def test_role_enforcement(self):
        plan = self.submit_clean()
        with self.assertRaises(ServiceError) as ctx:
            self.svc.tech_review(actor=CONTRACTOR, plan_id=plan["plan_id"],
                                 version=1, approve=True)
        self.assertEqual(ctx.exception.code, "FORBIDDEN")
        self.svc.tech_review(actor=TECH, plan_id=plan["plan_id"], version=1, approve=True)
        with self.assertRaises(ServiceError) as ctx:
            self.svc.ops_approve(actor=CONTRACTOR, plan_id=plan["plan_id"], version=1)
        self.assertEqual(ctx.exception.code, "FORBIDDEN")


if __name__ == "__main__":
    unittest.main()
