"""HTTP 接口端到端测试：启动真实服务器，经 HTTP JSON 走完整流程。"""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from tunnel_booking.api import create_server
from tunnel_booking.cli import MutableClock
from tunnel_booking.demo import build_demo_topology
from tunnel_booking.service import BookingService


def power_body(rid, start="2026-10-05T08:00", end="2026-10-05T18:00",
               segment="S1", chain=(20, 100), x=1.0):
    return {
        "request_id": rid,
        "contractor": "电力队",
        "window": {"start": start, "end": end},
        "envelopes": [{
            "envelope_id": f"{rid}-E1",
            "compartment_id": "C1",
            "segment_id": segment,
            "chainage": {"start_m": chain[0], "end_m": chain[1]},
            "footprint": {"x_m": x, "y_m": 1.0, "width_m": 0.4, "height_m": 0.4},
            "utility": "power",
        }],
        "depends_on": [],
    }


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clock = MutableClock(datetime(2026, 10, 4, 8, 0))
        from datetime import timedelta
        cls.server, cls.service = create_server(build_demo_topology(), "127.0.0.1", 0)
        # 换成可控时钟。
        cls.service._clock = cls.clock
        cls.service.audit._clock = cls.clock
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, body=None, actor="tester", expect_error=False):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json", "X-Actor": actor}
        req = urllib.request.Request(self.base + path, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            if expect_error:
                return exc.code, payload
            raise AssertionError(f"{method} {path} 意外失败: {exc.code} {payload}")

    def test_01_health_and_topology(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["data"]["status"], "up")
        status, body = self.request("GET", "/topology")
        comps = {c["compartment_id"] for c in body["data"]["compartments"]}
        self.assertEqual(comps, {"C1"})

    def test_02_full_normal_flow_over_http(self):
        status, body = self.request("POST", "/requests/submit", power_body("P-1"))
        self.assertEqual(body["data"]["status"], "SUBMITTED")
        self.request("POST", "/requests/P-1/technical-review",
                     {"approved": True, "comment": "ok"}, actor="eng")
        self.request("POST", "/requests/P-1/operational-approval",
                     {"approved": True}, actor="ops")
        status, body = self.request("POST", "/requests/P-1/activate", {})
        self.assertEqual(body["data"]["status"], "ACTIVE")

    def test_03_conflict_submission_returns_explanation(self):
        status, body = self.request("POST", "/requests/submit", power_body(
            "C-1", "2026-10-05T09:00", "2026-10-05T16:00", x=1.1))
        data = body["data"]
        self.assertFalse(data["accepted"])
        self.assertEqual(data["status"], "REJECTED")
        self.assertTrue(data["conflicts"])

    def test_04_idempotent_resubmit(self):
        body = power_body("P-2", "2026-10-20T08:00", "2026-10-20T10:00")
        _, first = self.request("POST", "/requests/submit", body)
        _, second = self.request("POST", "/requests/submit", body)
        self.assertTrue(second["data"]["idempotent"])
        self.assertEqual(first["data"]["version_no"], second["data"]["version_no"])

    def test_05_invalid_json_is_400(self):
        req = urllib.request.Request(
            self.base + "/requests/submit", data=b"{not json",
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)

    def test_06_unknown_request_is_404(self):
        code, body = self.request("GET", "/requests/NOPE", expect_error=True)
        self.assertEqual(code, 404)
        self.assertEqual(body["error"]["code"], "NOT_FOUND")

    def test_07_emergency_suspend_recover_via_http(self):
        # 准备一个 10-08 在场的给水许可。
        wbody = {
            "request_id": "W-9", "contractor": "给水班",
            "window": {"start": "2026-10-08T08:00", "end": "2026-10-08T16:00"},
            "envelopes": [{
                "envelope_id": "W-9-E1", "compartment_id": "C1", "segment_id": "S2",
                "chainage": {"start_m": 140, "end_m": 190},
                "footprint": {"x_m": 2.0, "y_m": 0.6, "width_m": 0.5, "height_m": 0.5},
                "utility": "water",
            }],
            "depends_on": [],
        }
        self.clock.advance(days=4)
        self.request("POST", "/requests/submit", wbody)
        self.request("POST", "/requests/W-9/technical-review", {"approved": True}, "eng")
        self.request("POST", "/requests/W-9/operational-approval", {"approved": True}, "ops")
        self.request("POST", "/requests/W-9/activate", {})
        # 抢修占用与其重叠的空间。
        emergency = {
            "emergency_id": "EMG-9",
            "reason": "爆管",
            "authorized": True,
            "proposal": {
                "request_id": "EMG-9-R", "contractor": "抢修队",
                "window": {"start": "2026-10-08T10:00", "end": "2026-10-08T12:00"},
                "envelopes": [{
                    "envelope_id": "EMG-9-E", "compartment_id": "C1", "segment_id": "S2",
                    "chainage": {"start_m": 145, "end_m": 195},
                    "footprint": {"x_m": 1.6, "y_m": 0.6, "width_m": 1.0, "height_m": 1.0},
                    "utility": "fire",
                }],
                "depends_on": [],
            },
        }
        _, body = self.request("POST", "/emergencies", emergency, actor="duty")
        self.assertIn("W-9", body["data"]["suspended_permits"])
        _, w9 = self.request("GET", "/requests/W-9")
        self.assertEqual(w9["data"]["status"], "SUSPENDED")
        _, rec = self.request("POST", "/emergencies/EMG-9/recover", {}, actor="duty")
        self.assertEqual(rec["data"]["restored_request_id"], "W-9")

    def test_08_unauthorized_emergency_is_403(self):
        body = {"emergency_id": "EMG-X", "authorized": False,
                "proposal": power_body("EMG-X-R", "2026-10-09T10:00", "2026-10-09T11:00")}
        code, payload = self.request("POST", "/emergencies", body, expect_error=True)
        self.assertEqual(code, 403)
        self.assertEqual(payload["error"]["code"], "PERMISSION_DENIED")

    def test_09_inspection_locks_until_pass(self):
        # 新建独立许可走完进场 -> 撤场验收。
        rid = "P-8"
        self.request("POST", "/requests/submit",
                     power_body(rid, "2026-10-10T08:00", "2026-10-10T12:00"))
        self.request("POST", f"/requests/{rid}/technical-review", {"approved": True}, "eng")
        self.request("POST", f"/requests/{rid}/operational-approval", {"approved": True}, "ops")
        self.request("POST", f"/requests/{rid}/activate", {})
        code, _ = self.request("POST", f"/requests/{rid}/withdraw", {}, expect_error=True)
        self.assertEqual(code, 423)
        self.request("POST", f"/requests/{rid}/inspection/request", {})
        _, fail = self.request("POST", f"/requests/{rid}/inspection/complete",
                               {"accepted": False, "comment": "整改"}, actor="insp")
        self.assertEqual(fail["data"]["status"], "ACTIVE")
        self.request("POST", f"/requests/{rid}/inspection/request", {})
        _, done = self.request("POST", f"/requests/{rid}/inspection/complete",
                               {"accepted": True}, actor="insp")
        self.assertEqual(done["data"]["status"], "COMPLETED")

    def test_10_audit_endpoint_tells_full_story(self):
        _, body = self.request("GET", "/audit?entity_id=EMG-9")
        actions = {e["action"] for e in body["data"]["events"]}
        self.assertIn("EMERGENCY_OCCUPY", actions)
        _, all_events = self.request("GET", "/audit")
        seqs = [e["seq"] for e in all_events["data"]["events"]]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


if __name__ == "__main__":
    unittest.main()
