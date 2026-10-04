"""基于标准库 http.server 的 REST 接口。

身份通过请求头传递（演示级）：
  X-Actor-Name / X-Actor-Roles(逗号分隔) / X-Actor-Emergency-Level
幂等通过 Idempotency-Key 请求头传递。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from .service import GalleryService, ServiceError


def _actor_from_headers(headers) -> dict:
    # 头部仅支持 latin-1，中文名按 percent-encoding 传输，这里解码
    return {
        "name": unquote(headers.get("X-Actor-Name", "anonymous")),
        "roles": [unquote(r.strip()) for r in headers.get("X-Actor-Roles", "").split(",") if r.strip()],
        "emergency_level": int(headers.get("X-Actor-Emergency-Level", "0") or 0),
    }


ROUTES = [
    ("POST", re.compile(r"^/sections$"), "add_section"),
    ("GET", re.compile(r"^/sections$"), "list_sections"),
    ("GET", re.compile(r"^/sections/(?P<id>[^/]+)/occupancy$"), "section_occupancy"),
    ("POST", re.compile(r"^/plans$"), "submit_plan"),
    ("GET", re.compile(r"^/plans/(?P<id>[^/]+)$"), "get_plan"),
    ("GET", re.compile(r"^/plans/(?P<id>[^/]+)/conflicts$"), "plan_conflicts"),
    ("POST", re.compile(r"^/plans/(?P<id>[^/]+)/amend$"), "amend_plan"),
    ("POST", re.compile(r"^/plans/(?P<id>[^/]+)/tech-review$"), "tech_review"),
    ("POST", re.compile(r"^/plans/(?P<id>[^/]+)/ops-approve$"), "ops_approve"),
    ("GET", re.compile(r"^/permits$"), "list_permits"),
    ("GET", re.compile(r"^/permits/(?P<id>[^/]+)$"), "get_permit"),
    ("POST", re.compile(r"^/permits/(?P<id>[^/]+)/check-in$"), "check_in"),
    ("POST", re.compile(r"^/permits/(?P<id>[^/]+)/acceptance-request$"), "request_acceptance"),
    ("POST", re.compile(r"^/permits/(?P<id>[^/]+)/acceptance$"), "complete_acceptance"),
    ("GET", re.compile(r"^/permits/(?P<id>[^/]+)/recovery-plan$"), "recovery_plan"),
    ("POST", re.compile(r"^/emergency/occupy$"), "emergency_occupy"),
    ("GET", re.compile(r"^/audit$"), "audit_trail"),
    ("GET", re.compile(r"^/audit/verify$"), "verify_audit"),
]


def make_handler(service: GalleryService):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # 静默
            pass

        def _send(self, status: int, obj) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _dispatch(self, method: str) -> None:
            actor = _actor_from_headers(self.headers)
            try:
                body = self._body() if method == "POST" else {}
            except json.JSONDecodeError:
                return self._send(400, {"error": {"code": "BAD_JSON", "message": "请求体不是合法 JSON"}})
            query = {}
            path = self.path
            if "?" in path:
                path, qs = path.split("?", 1)
                for pair in qs.split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        query[k] = v
            for m, pattern, name in ROUTES:
                if m != method:
                    continue
                match = pattern.match(path)
                if not match:
                    continue
                try:
                    result = self._call(name, actor, body, query, match.groupdict())
                    status = 201 if method == "POST" else 200
                    return self._send(status, result)
                except ServiceError as exc:
                    code = {
                        "NOT_FOUND": 404, "FORBIDDEN": 403, "CONFLICT": 409,
                        "IDEMPOTENCY_CONFLICT": 409, "BAD_STATE": 409,
                        "STALE_VERSION": 409, "APPROVAL_EXPIRED": 409,
                        "DEPENDENCY": 409, "VALIDATION": 400,
                    }.get(exc.code, 400)
                    return self._send(code, {"error": exc.to_dict()})
                except (KeyError, ValueError, TypeError) as exc:
                    return self._send(400, {"error": {"code": "BAD_REQUEST", "message": str(exc)}})
            return self._send(404, {"error": {"code": "NOT_FOUND", "message": f"无此路由：{method} {path}"}})

        def _call(self, name: str, actor: dict, body: dict, query: dict, path: dict):
            idem = self.headers.get("Idempotency-Key")
            if name == "add_section":
                return service.add_section(actor=actor, section=body)
            if name == "list_sections":
                return {"sections": service.list_sections()}
            if name == "section_occupancy":
                return service.section_occupancy(path["id"])
            if name == "submit_plan":
                return service.submit_plan(
                    actor=actor, occupancies=body.get("occupancies", []),
                    depends_on=body.get("depends_on", []), idempotency_key=idem)
            if name == "get_plan":
                version = query.get("version")
                return service.get_plan(path["id"], int(version) if version else None)
            if name == "plan_conflicts":
                version = query.get("version")
                return service.plan_conflicts(path["id"], int(version) if version else None)
            if name == "amend_plan":
                return service.amend_plan(
                    actor=actor, plan_id=path["id"], occupancies=body.get("occupancies", []),
                    depends_on=body.get("depends_on"), idempotency_key=idem)
            if name == "tech_review":
                return service.tech_review(
                    actor=actor, plan_id=path["id"], version=int(body["version"]),
                    approve=bool(body.get("approve", True)), comment=body.get("comment", ""))
            if name == "ops_approve":
                return service.ops_approve(
                    actor=actor, plan_id=path["id"], version=int(body["version"]),
                    valid_hours=float(body.get("valid_hours", 24.0)))
            if name == "list_permits":
                return {"permits": service.list_permits()}
            if name == "get_permit":
                return service.get_permit(path["id"])
            if name == "check_in":
                return service.check_in(actor=actor, permit_id=path["id"])
            if name == "request_acceptance":
                return service.request_acceptance(actor=actor, permit_id=path["id"])
            if name == "complete_acceptance":
                return service.complete_acceptance(
                    actor=actor, permit_id=path["id"],
                    passed=bool(body.get("passed", True)), comment=body.get("comment", ""))
            if name == "recovery_plan":
                return service.recovery_plan(path["id"])
            if name == "emergency_occupy":
                return service.emergency_occupy(
                    actor=actor, occupancies=body.get("occupancies", []),
                    reason=body.get("reason", ""), idempotency_key=idem)
            if name == "audit_trail":
                return {"audit": service.audit_trail(
                    query.get("entity_type"), query.get("entity_id"))}
            if name == "verify_audit":
                return service.verify_audit()
            raise ServiceError("NOT_FOUND", f"未知操作 {name}", {})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def make_server(service: GalleryService, host: str = "127.0.0.1", port: int = 8080):
    return ThreadingHTTPServer((host, port), make_handler(service))


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:
    service = GalleryService(db_path)
    server = make_server(service, host, port)
    print(f"管廊预约与作业许可服务已启动：http://{host}:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
