"""HTTP 接口：基于标准库 http.server，零第三方依赖。

路由：
  GET  /health
  GET  /topology
  POST /requests/submit
  GET  /requests                       列表
  GET  /requests/{id}
  POST /requests/{id}/technical-review
  POST /requests/{id}/operational-approval
  POST /requests/{id}/activate
  POST /requests/{id}/withdraw
  POST /requests/{id}/inspection/request
  POST /requests/{id}/inspection/complete
  POST /emergencies                    紧急抢修占用
  POST /emergencies/{id}/recover       按恢复顺序恢复下一个许可
  POST /emergencies/{id}/close
  GET  /emergencies/{id}
  GET  /audit[?entity_type=&entity_id=&action=&actor=]
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .errors import BookingError
from .serialization import proposal_from_dict
from .service import BookingService
from .topology import GalleryTopology

ERROR_STATUS = {
    "NOT_FOUND": 404,
    "INVALID_STATE": 409,
    "PERMISSION_DENIED": 403,
    "APPROVAL_EXPIRED": 410,
    "OCCUPANCY_LOCKED": 423,
    "IDEMPOTENCY_CONFLICT": 409,
    "VALIDATION_ERROR": 400,
    "BOOKING_ERROR": 400,
}


class ApiHandler(BaseHTTPRequestHandler):
    service: Optional[BookingService] = None

    # ------------------------------------------------------------ 基础框架

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BookingError(f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise BookingError("请求体必须是 JSON 对象")
        return data

    def _run(self, fn: Callable[[], dict], status: int = 200) -> None:
        try:
            result = fn()
            self._send(status, {"ok": True, "data": result})
        except BookingError as exc:
            code = getattr(exc, "code", "BOOKING_ERROR")
            self._send(ERROR_STATUS.get(code, 400),
                       {"ok": False, "error": {"code": code, "message": str(exc)}})
        except (ValueError, KeyError, TypeError) as exc:
            self._send(400, {"ok": False,
                             "error": {"code": "VALIDATION_ERROR", "message": str(exc)}})

    # ----------------------------------------------------------------- GET

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        q = parse_qs(parsed.query)

        def scalar(name: str):
            vals = q.get(name)
            return vals[0] if vals else None

        if parts == ["health"]:
            self._send(200, {"ok": True, "data": {"status": "up"}})
        elif parts == ["topology"]:
            self._run(lambda: self.service.topology.to_dict())
        elif parts == ["requests"]:
            self._run(lambda: {"requests": self.service.list_requests()})
        elif len(parts) == 2 and parts[0] == "requests":
            self._run(lambda: self.service.get_request(parts[1]))
        elif len(parts) == 2 and parts[0] == "emergencies":
            self._run(lambda: self.service.get_emergency(parts[1]))
        elif parts == ["audit"]:
            self._run(lambda: {
                "events": [
                    e.to_dict() for e in self.service.audit.query(
                        entity_type=scalar("entity_type"),
                        entity_id=scalar("entity_id"),
                        action=scalar("action"),
                        actor=scalar("actor"),
                    )
                ]
            })
        else:
            self._send(404, {"ok": False, "error": {"code": "NOT_FOUND",
                                                    "message": parsed.path}})

    # ---------------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        svc = self.service

        if parts == ["requests", "submit"]:
            self._run(lambda: svc.submit(proposal_from_dict(self._read_json()),
                                         actor=self._actor()))
        elif parts == ["emergencies"]:
            def _emergency() -> dict:
                d = self._read_json()
                return svc.emergency_occupy(
                    proposal_from_dict(d["proposal"]),
                    emergency_id=d["emergency_id"],
                    declarer=self._actor(),
                    reason=d.get("reason", ""),
                    authorized=bool(d.get("authorized", True)),
                )
            self._run(_emergency)
        elif len(parts) == 3 and parts[0] == "requests":
            rid, action = parts[1], parts[2]
            if action == "technical-review":
                self._run(lambda: self._bool_review(
                    lambda approved, comment: svc.technical_review(
                        rid, self._actor(), approved, comment)))
            elif action == "operational-approval":
                self._run(lambda: self._bool_review(
                    lambda approved, comment: svc.operational_approval(
                        rid, self._actor(), approved, comment)))
            elif action == "activate":
                self._run(lambda: svc.activate(rid, self._actor()))
            elif action == "withdraw":
                self._run(lambda: svc.withdraw(rid, self._actor()))
            else:
                self._not_found(parsed.path)
        elif len(parts) == 4 and parts[0] == "requests" and parts[2] == "inspection":
            rid, action = parts[1], parts[3]
            if action == "request":
                self._run(lambda: svc.request_inspection(rid, self._actor()))
            elif action == "complete":
                def _complete() -> dict:
                    d = self._read_json()
                    return svc.complete_inspection(
                        rid, self._actor(), bool(d.get("accepted", True)),
                        d.get("comment", ""))
                self._run(_complete)
            else:
                self._not_found(parsed.path)
        elif len(parts) == 3 and parts[0] == "emergencies":
            eid, action = parts[1], parts[2]
            if action == "recover":
                self._run(lambda: svc.recover_next(eid, self._actor()))
            elif action == "close":
                self._run(lambda: svc.close_emergency(eid, self._actor()))
            else:
                self._not_found(parsed.path)
        else:
            self._not_found(parsed.path)

    # ------------------------------------------------------------- 小工具

    def _actor(self) -> str:
        return self.headers.get("X-Actor", "anonymous")

    def _bool_review(self, fn: Callable) -> dict:
        d = self._read_json()
        return fn(bool(d.get("approved", True)), d.get("comment", ""))

    def _not_found(self, path: str) -> None:
        self._send(404, {"ok": False, "error": {"code": "NOT_FOUND", "message": path}})


def create_server(topology: GalleryTopology, host: str = "127.0.0.1",
                  port: int = 8080) -> Tuple[ThreadingHTTPServer, BookingService]:
    from .service import BookingService

    service = BookingService(topology)
    handler = type("BoundApiHandler", (ApiHandler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    server.service = service  # type: ignore[attr-defined]
    return server, service


def main() -> None:
    import argparse

    from .demo import build_demo_topology

    parser = argparse.ArgumentParser(description="管廊空间预约与作业许可 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server, _ = create_server(build_demo_topology(), args.host, args.port)
    print(f"管廊预约服务已启动: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
