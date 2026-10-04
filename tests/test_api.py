"""通过 REST 接口验证：跨区段预约、紧急抢修、许可恢复与完整审计记录。"""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import requests
from urllib.parse import quote

from helpers import corridor_sections, occ
from tunnel_booking.api import make_server
from tunnel_booking.service import GalleryService

# HTTP 头仅支持 latin-1，中文名按 percent-encoding 传输
OPS_HEADERS = {"X-Actor-Name": quote("运营"), "X-Actor-Roles": "ops_approver,acceptor"}
TECH_HEADERS = {"X-Actor-Name": quote("复核"), "X-Actor-Roles": "tech_reviewer"}
REPAIR_HEADERS = {"X-Actor-Name": quote("抢修队"), "X-Actor-Emergency-Level": "2"}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(GalleryService(":memory:"), "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_full_scenario_over_http(self):
        # 建廊：两段连续区段
        for section in corridor_sections():
            r = requests.post(f"{self.base}/sections", json=section, headers=OPS_HEADERS)
            self.assertEqual(r.status_code, 201, r.text)

        # 1) 跨区段预约：K0+150~K0+260 跨越 SEC-A/SEC-B
        r = requests.post(f"{self.base}/plans", headers={**TECH_HEADERS, "Idempotency-Key": "API-K1"},
                          json={"occupancies": [occ("RQ-A", 150, 260, 1.0, 1.0)]})
        self.assertEqual(r.status_code, 201, r.text)
        plan = r.json()
        self.assertEqual(plan["blocking_conflicts"], 0)
        plan_id = plan["plan_id"]

        # 幂等：相同键重复提交返回原结果
        r = requests.post(f"{self.base}/plans", headers={**TECH_HEADERS, "Idempotency-Key": "API-K1"},
                          json={"occupancies": [occ("RQ-A", 150, 260, 1.0, 1.0)]})
        self.assertEqual(r.json()["plan_id"], plan_id)
        self.assertTrue(r.json()["idempotent_replay"])

        # 审批 + 进场
        r = requests.post(f"{self.base}/plans/{plan_id}/tech-review",
                          json={"version": 1, "approve": True}, headers=TECH_HEADERS)
        self.assertEqual(r.status_code, 201, r.text)
        r = requests.post(f"{self.base}/plans/{plan_id}/ops-approve",
                          json={"version": 1, "valid_hours": 24}, headers=OPS_HEADERS)
        self.assertEqual(r.status_code, 201, r.text)
        permit_id = r.json()["permit"]["permit_id"]
        r = requests.post(f"{self.base}/permits/{permit_id}/check-in", headers=OPS_HEADERS)
        self.assertEqual(r.json()["status"], "ACTIVE")

        # 区段占用查询可见该许可
        r = requests.get(f"{self.base}/sections/SEC-B/occupancy")
        self.assertTrue(any(o["permit_id"] == permit_id
                            for o in r.json()["occupancies"]))

        # 2) 紧急抢修：L2 权限临时占用，自动暂停受影响许可
        r = requests.post(f"{self.base}/emergency/occupy", headers=REPAIR_HEADERS, json={
            "reason": "K0+180 爆管",
            "occupancies": [{
                "request_id": "RQ-E1", "chainage_start_m": 160, "chainage_end_m": 240,
                "rect": {"x_m": 0.6, "y_m": 0.0, "width_m": 1.6, "height_m": 2.0},
                "utility": "WATER", "process": "EMERGENCY",
                "time_start": "2026-10-05T11:00:00+00:00",
                "time_end": "2026-10-05T15:00:00+00:00",
            }],
        })
        self.assertEqual(r.status_code, 201, r.text)
        em = r.json()
        em_id = em["permit"]["permit_id"]
        self.assertEqual([s["permit_id"] for s in em["suspended"]], [permit_id])
        r = requests.get(f"{self.base}/permits/{permit_id}")
        self.assertEqual(r.json()["status"], "SUSPENDED")

        # 3) 许可恢复：抢修撤场验收后按恢复顺序恢复
        requests.post(f"{self.base}/permits/{em_id}/acceptance-request", headers=REPAIR_HEADERS)
        r = requests.post(f"{self.base}/permits/{em_id}/acceptance",
                          json={"passed": True}, headers=OPS_HEADERS)
        self.assertEqual(r.json()["status"], "COMPLETED")
        r = requests.get(f"{self.base}/permits/{em_id}/recovery-plan")
        self.assertEqual(r.json()["recovery"][0]["status"], "RESUMED")
        r = requests.get(f"{self.base}/permits/{permit_id}")
        self.assertEqual(r.json()["status"], "ACTIVE")

        # 4) 完整审计记录：哈希链完整且关键动作齐全
        r = requests.get(f"{self.base}/audit/verify")
        self.assertTrue(r.json()["ok"])
        r = requests.get(f"{self.base}/audit", params={"entity_id": permit_id})
        actions = [a["action"] for a in r.json()["audit"]]
        self.assertIn("CHECKED_IN", actions)
        self.assertIn("PERMIT_SUSPENDED", actions)
        self.assertIn("PERMIT_RESUMED", actions)

    def test_error_shape(self):
        r = requests.get(f"{self.base}/permits/PM-9999")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
