import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from helpers import CONTRACTOR, OPS, approve_and_check_in, build_service, occ


class AuditTests(unittest.TestCase):
    def test_audit_records_full_lifecycle(self):
        svc = build_service()
        plan = svc.submit_plan(actor=CONTRACTOR, occupancies=[occ("RQ-1", 100, 180, 1.0, 1.0)])
        permit_id = approve_and_check_in(svc, plan["plan_id"])
        svc.request_acceptance(actor=CONTRACTOR, permit_id=permit_id)
        svc.complete_acceptance(actor=OPS, permit_id=permit_id, passed=True)

        actions = [a["action"] for a in svc.audit_trail()]
        for expected in ["SECTION_UPSERTED", "PLAN_SUBMITTED", "TECH_REVIEWED",
                         "OPS_APPROVED", "CHECKED_IN", "ACCEPTANCE_REQUESTED",
                         "ACCEPTANCE_PASSED"]:
            self.assertIn(expected, actions)
        # 可按实体检索
        permit_trail = svc.audit_trail(entity_type="permit", entity_id=permit_id)
        self.assertTrue(all(a["entity_id"] == permit_id for a in permit_trail))
        self.assertGreaterEqual(len(permit_trail), 4)
        # 每条记录都带操作人与时间
        self.assertTrue(all(a["actor"] and a["ts"] for a in svc.audit_trail()))

    def test_audit_hash_chain_verifies(self):
        svc = build_service()
        plan = svc.submit_plan(actor=CONTRACTOR, occupancies=[occ("RQ-1", 100, 180, 1.0, 1.0)])
        approve_and_check_in(svc, plan["plan_id"])
        self.assertTrue(svc.verify_audit()["ok"])

    def test_audit_tamper_is_detected(self):
        svc = build_service()
        svc.submit_plan(actor=CONTRACTOR, occupancies=[occ("RQ-1", 100, 180, 1.0, 1.0)])
        # 直接篡改数据库中的审计内容
        svc.store._conn.execute(
            "UPDATE audit SET details = ? WHERE seq = 1",
            (json.dumps({"forged": True}, ensure_ascii=False),),
        )
        svc.store._conn.commit()
        result = svc.verify_audit()
        self.assertFalse(result["ok"])
        self.assertEqual(result["broken_at_seq"], 1)


if __name__ == "__main__":
    unittest.main()
