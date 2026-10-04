"""BookingService 全流程测试：版本/幂等/审批过期/抢修/恢复/撤场。"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from tunnel_booking.cli import MutableClock, env, proposal
from tunnel_booking.contracts import UtilityKind
from tunnel_booking.demo import build_demo_topology
from tunnel_booking.errors import (
    ApprovalExpiredError,
    InvalidStateError,
    NotFoundError,
    OccupancyLockedError,
    PermissionDeniedError,
)
from tunnel_booking.models import PermitStatus
from tunnel_booking.service import BookingService


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.clock = MutableClock(datetime(2026, 10, 4, 8, 0))
        self.svc = BookingService(build_demo_topology(), clock=self.clock,
                                  approval_ttl=timedelta(hours=24))

    def power_proposal(self, rid="P-1", start="2026-10-05T08:00",
                       end="2026-10-05T18:00", rect=(1.0, 1.0, 0.4, 0.4),
                       chain=(20.0, 100.0), depends_on=()):
        return proposal(rid, "电力队", start, end,
                        [env("E1", UtilityKind.POWER, "S1", chain[0], chain[1],
                             rect[0], rect[1], rect[2], rect[3])],
                        depends_on=depends_on)

    def approve_and_activate(self, rid):
        self.svc.technical_review(rid, "eng", True)
        self.svc.operational_approval(rid, "ops", True)
        self.svc.activate(rid, "crew")


class SubmissionTests(ServiceTestBase):
    def test_clean_submission_enters_review(self):
        r = self.svc.submit(self.power_proposal(), "crew")
        self.assertTrue(r["accepted"])
        self.assertEqual(r["status"], "SUBMITTED")
        self.assertEqual(r["version_no"], 1)

    def test_conflicting_submission_rejected_and_explained(self):
        self.svc.submit(self.power_proposal("P-1"), "crew-a")
        r = self.svc.submit(proposal(
            "C-1", "通信队", "2026-10-05T09:00", "2026-10-05T17:00",
            [env("E1", UtilityKind.COMM, "S1", 30, 90, 1.1, 1.0, 0.4, 0.4)]), "crew-b")
        self.assertFalse(r["accepted"])
        self.assertEqual(r["status"], "REJECTED")
        self.assertTrue(r["conflicts"])
        self.assertTrue(r["conflicts"][0]["message"])

    def test_idempotent_resubmit_returns_original_version(self):
        p = self.power_proposal()
        r1 = self.svc.submit(p, "crew")
        r2 = self.svc.submit(p, "crew")
        self.assertTrue(r2["idempotent"])
        self.assertEqual(r1["version_no"], r2["version_no"])
        # 审计应记录幂等重放，而不是再建一个版本。
        self.assertEqual(self.svc.get_request("P-1")["current_version"], 1)

    def test_change_requires_base_version(self):
        self.svc.submit(self.power_proposal(), "crew")
        with self.assertRaises(InvalidStateError):
            self.svc.submit(self.power_proposal(
                rect=(1.2, 1.2, 0.3, 0.3)), "crew")
        # 显式基于 v1 提交 v2，允许。
        r = self.svc.submit(self.power_proposal(rect=(1.2, 1.2, 0.3, 0.3)),
                            "crew", base_version=1)
        self.assertEqual(r["version_no"], 2)
        self.assertEqual(r["base_version"], 1)

    def test_change_against_stale_base_rejected(self):
        self.svc.submit(self.power_proposal(), "crew")
        self.svc.submit(self.power_proposal(rect=(1.2, 1.2, 0.3, 0.3)),
                        "crew", base_version=1)
        with self.assertRaises(InvalidStateError):
            self.svc.submit(self.power_proposal(rect=(1.3, 1.3, 0.3, 0.3)),
                            "crew", base_version=1)

    def test_submit_unknown_request_with_base(self):
        with self.assertRaises(NotFoundError):
            self.svc.submit(self.power_proposal("NOPE"), "crew", base_version=9)


class ApprovalFlowTests(ServiceTestBase):
    def test_two_stage_approval_and_activate(self):
        self.svc.submit(self.power_proposal(), "crew")
        self.assertEqual(self.svc.technical_review("P-1", "eng", True)["status"],
                         "TECH_APPROVED")
        self.assertEqual(self.svc.operational_approval("P-1", "ops", True)["status"],
                         "APPROVED")
        self.assertEqual(self.svc.activate("P-1", "crew")["status"], "ACTIVE")

    def test_cannot_skip_technical_review(self):
        self.svc.submit(self.power_proposal(), "crew")
        with self.assertRaises(InvalidStateError):
            self.svc.operational_approval("P-1", "ops", True)

    def test_technical_reject_blocks_pipeline(self):
        self.svc.submit(self.power_proposal(), "crew")
        self.svc.technical_review("P-1", "eng", False, "资料不全")
        with self.assertRaises(InvalidStateError):
            self.svc.operational_approval("P-1", "ops", True)

    def test_expired_approval_cannot_activate(self):
        self.svc.submit(self.power_proposal(), "crew")
        self.svc.technical_review("P-1", "eng", True)
        self.svc.operational_approval("P-1", "ops", True)
        self.clock.advance(hours=25)
        with self.assertRaises(ApprovalExpiredError):
            self.svc.activate("P-1", "crew")
        self.assertEqual(self.svc.status_of("P-1"), PermitStatus.EXPIRED)

    def test_expired_version_blocks_even_after_resubmit_same_body(self):
        # 过期后用相同方案体重提：按“重复请求返回原结果”返回过期版本，
        # 旧审批仍然不能进场生效；要继续办理必须基于版本提交变更版重新审批。
        p = self.power_proposal()
        self.svc.submit(p, "crew")
        self.svc.technical_review("P-1", "eng", True)
        self.svc.operational_approval("P-1", "ops", True)
        self.clock.advance(hours=25)
        with self.assertRaises(ApprovalExpiredError):
            self.svc.activate("P-1", "crew")
        replay = self.svc.submit(p, "crew")
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["status"], "EXPIRED")
        with self.assertRaises(ApprovalExpiredError):
            self.svc.activate("P-1", "crew")


class CrossSegmentTests(ServiceTestBase):
    def test_cross_segment_reservation_holds_both_segments(self):
        p = proposal("P-X", "电力队", "2026-10-05T08:00", "2026-10-05T18:00",
                     [env("E1", UtilityKind.POWER, "S1", 60, 120, 1.0, 1.0, 0.4, 0.4),
                      env("E2", UtilityKind.POWER, "S2", 120, 180, 1.0, 1.0, 0.4, 0.4)])
        r = self.svc.submit(p, "crew")
        self.assertTrue(r["accepted"])
        # 另一施工方在 S2 150 处同一时间占位 -> 必须能识别跨区段冲突。
        r2 = self.svc.submit(proposal(
            "C-X", "通信队", "2026-10-05T09:00", "2026-10-05T12:00",
            [env("E3", UtilityKind.COMM, "S2", 150, 170, 1.05, 1.0, 0.4, 0.4)]), "crew2")
        self.assertFalse(r2["accepted"])
        self.assertTrue(any(c["code"] == "SPATIAL_OVERLAP" for c in r2["conflicts"]))


class EmergencyTests(ServiceTestBase):
    def _setup_two_active(self):
        # 给水 W（S2）与消防 F（S2），10-07 同时在场，截面错开净距达标。
        wp = proposal("W", "给水班", "2026-10-07T08:00", "2026-10-07T16:00",
                      [env("EW", UtilityKind.WATER, "S2", 140, 190, 2.0, 0.6, 0.5, 0.5)])
        fp = proposal("F", "消防班", "2026-10-07T09:00", "2026-10-07T15:00",
                      [env("EF", UtilityKind.FIRE, "S2", 150, 200, 1.0, 1.6, 0.4, 0.4)])
        self.clock.advance(days=3)
        for p in (wp, fp):
            self.svc.submit(p, "crew")
            self.approve_and_activate(p.request_id)

    def test_emergency_suspends_affected_and_orders_recovery(self):
        self._setup_two_active()
        repair = proposal("EMG", "抢修队", "2026-10-07T10:00", "2026-10-07T12:00",
                          [env("ER", UtilityKind.FIRE, "S2", 145, 195, 1.2, 0.8, 1.2, 1.2)])
        r = self.svc.emergency_occupy(repair, "EMG-1", "duty", "泄漏抢修", True)
        self.assertTrue(r["accepted"])
        self.assertEqual(set(r["suspended_permits"]), {"W", "F"})
        self.assertEqual(r["recovery_order"][0], "F")  # 消防优先恢复
        self.assertEqual(self.svc.status_of("W"), PermitStatus.SUSPENDED)
        self.assertEqual(self.svc.status_of("F"), PermitStatus.SUSPENDED)

        r1 = self.svc.recover_next("EMG-1", "duty")
        self.assertEqual(r1["restored_request_id"], "F")
        r2 = self.svc.recover_next("EMG-1", "duty")
        self.assertEqual(r2["restored_request_id"], "W")
        with self.assertRaises(InvalidStateError):
            self.svc.recover_next("EMG-1", "duty")

        view = self.svc.get_emergency("EMG-1")
        self.assertEqual(view["restored"], ["F", "W"])

    def test_unauthorized_emergency_denied(self):
        repair = self.power_proposal("EMG", "2026-10-07T10:00", "2026-10-07T12:00")
        with self.assertRaises(PermissionDeniedError):
            self.svc.emergency_occupy(repair, "EMG-2", "outsider", "", authorized=False)

    def test_emergency_rejected_on_topology_violation(self):
        # 抢修也不能压安全通道。
        repair = proposal("EMG", "抢修队", "2026-10-07T10:00", "2026-10-07T12:00",
                          [env("ER", UtilityKind.WATER, "S1", 0, 20, 0.0, 0.0, 0.8, 1.0)])
        r = self.svc.emergency_occupy(repair, "EMG-3", "duty", "违规抢修", True)
        self.assertFalse(r["accepted"])
        self.assertTrue(any(c["code"] == "SAFETY_PASSAGE_BLOCKED"
                            for c in r["conflicts"]))

    def test_suspended_permit_cannot_release_occupancy(self):
        self._setup_two_active()
        repair = proposal("EMG", "抢修队", "2026-10-07T10:00", "2026-10-07T12:00",
                          [env("ER", UtilityKind.FIRE, "S2", 145, 195, 1.2, 0.8, 1.2, 1.2)])
        self.svc.emergency_occupy(repair, "EMG-1", "duty", "", True)
        with self.assertRaises(OccupancyLockedError):
            self.svc.withdraw("W", "crew")


class InspectionTests(ServiceTestBase):
    def test_occupancy_held_until_inspection_passed(self):
        self.clock.advance(days=1)
        self.svc.submit(self.power_proposal(), "crew")
        self.approve_and_activate("P-1")
        # 在场不能直接撤回。
        with self.assertRaises(OccupancyLockedError):
            self.svc.withdraw("P-1", "crew")
        self.svc.request_inspection("P-1", "crew")
        self.assertTrue(self.svc.get_request("P-1")["holding_occupancy"])
        # 验收不合格：占位仍不释放，退回 ACTIVE。
        self.svc.complete_inspection("P-1", "insp", False, "需整改")
        self.assertEqual(self.svc.status_of("P-1"), PermitStatus.ACTIVE)
        self.svc.request_inspection("P-1", "crew")
        self.svc.complete_inspection("P-1", "insp", True, "合格")
        self.assertEqual(self.svc.status_of("P-1"), PermitStatus.COMPLETED)
        self.assertFalse(self.svc.get_request("P-1")["holding_occupancy"])

    def test_completed_permit_releases_space_for_next_job(self):
        self.clock.advance(days=1)
        self.svc.submit(self.power_proposal("P-1", "2026-10-06T08:00",
                                            "2026-10-06T10:00"), "crew")
        self.approve_and_activate("P-1")
        self.svc.request_inspection("P-1", "crew")
        self.svc.complete_inspection("P-1", "insp", True)
        # 同空间、同时间窗的新申请此时应无冲突（占位已释放）。
        r = self.svc.submit(self.power_proposal(
            "P-2", "2026-10-06T08:00", "2026-10-06T10:00"), "crew2")
        self.assertTrue(r["accepted"])


class AuditTests(ServiceTestBase):
    def test_audit_trail_is_complete_and_append_only_ordered(self):
        self.svc.submit(self.power_proposal(), "crew")
        self.svc.technical_review("P-1", "eng", True)
        self.svc.operational_approval("P-1", "ops", True)
        self.svc.activate("P-1", "crew")
        events = self.svc.audit.to_list()
        self.assertEqual([e["seq"] for e in events], [1, 2, 3, 4])
        self.assertEqual([e["action"] for e in events],
                         ["PROPOSAL_SUBMIT", "TECH_REVIEW_APPROVE",
                          "OP_APPROVE", "PERMIT_ACTIVATE"])
        # 可按实体过滤。
        self.assertEqual(len(self.svc.audit.query(entity_id="P-1")), 4)
        self.assertEqual(len(self.svc.audit.query(actor="eng")), 1)
        # 驳回路径也落审计。
        self.svc.submit(self.power_proposal("P-9", "2026-10-09T08:00",
                                            "2026-10-09T10:00"), "crew")
        self.svc.technical_review("P-9", "eng", False)
        self.assertTrue(any(e["action"] == "TECH_REVIEW_REJECT"
                            for e in self.svc.audit.to_list()))


if __name__ == "__main__":
    unittest.main()
