"""命令行入口：管廊空间预约与作业许可系统。

示例：
  python -m tunnel_booking.cli --db gallery.db add-section --file section.json
  python -m tunnel_booking.cli --db gallery.db submit --file plan.json --idempotency-key K1
  python -m tunnel_booking.cli --db gallery.db demo
"""
from __future__ import annotations

import argparse
import json
import sys

from .demo import run_demo
from .service import GalleryService, ServiceError


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _load_file(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tunnel-booking", description="综合管廊空间预约与作业许可系统")
    p.add_argument("--db", default="gallery.db", help="SQLite 数据库路径（默认 gallery.db）")
    p.add_argument("--actor", default="cli-user", help="操作人姓名")
    p.add_argument("--roles", default="", help="角色，逗号分隔：tech_reviewer,ops_approver,acceptor")
    p.add_argument("--emergency-level", type=int, default=0, help="抢修权限等级")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add-section", help="登记/更新连续区段（含横截面与既有设施分区）")
    s.add_argument("--file", required=True, help="区段 JSON 文件")

    sub.add_parser("sections", help="列出全部区段")

    s = sub.add_parser("occupancy", help="查看某区段当前占位")
    s.add_argument("--section", required=True)

    s = sub.add_parser("submit", help="提交施工方案（计算空间/时间冲突）")
    s.add_argument("--file", required=True, help="方案 JSON：{occupancies:[...], depends_on:[...]}")
    s.add_argument("--idempotency-key", default=None)

    s = sub.add_parser("amend", help="基于当前版本提交方案变更（生成新版本）")
    s.add_argument("--plan", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--idempotency-key", default=None)

    s = sub.add_parser("plan", help="查看方案")
    s.add_argument("--plan", required=True)
    s.add_argument("--version", type=int, default=None)

    s = sub.add_parser("conflicts", help="重新计算方案冲突")
    s.add_argument("--plan", required=True)
    s.add_argument("--version", type=int, default=None)

    s = sub.add_parser("tech-review", help="技术复核")
    s.add_argument("--plan", required=True)
    s.add_argument("--version", type=int, required=True)
    s.add_argument("--reject", action="store_true", help="退回而非通过")
    s.add_argument("--comment", default="")

    s = sub.add_parser("ops-approve", help="运营批准（生成作业许可，含有效期）")
    s.add_argument("--plan", required=True)
    s.add_argument("--version", type=int, required=True)
    s.add_argument("--valid-hours", type=float, default=24.0)

    s = sub.add_parser("check-in", help="许可进场登记（校验审批未过期、无新冲突）")
    s.add_argument("--permit", required=True)

    s = sub.add_parser("acceptance-request", help="撤场并申请验收")
    s.add_argument("--permit", required=True)

    s = sub.add_parser("acceptance", help="撤场验收（通过后才释放占位）")
    s.add_argument("--permit", required=True)
    s.add_argument("--fail", action="store_true", help="验收不通过，退回整改")
    s.add_argument("--comment", default="")

    s = sub.add_parser("emergency", help="紧急抢修临时占用（自动暂停受影响许可并生成恢复顺序）")
    s.add_argument("--file", required=True, help="抢修占位 JSON：{occupancies:[...], reason:...}")
    s.add_argument("--idempotency-key", default=None)

    s = sub.add_parser("recovery-plan", help="查看抢修后的许可恢复顺序")
    s.add_argument("--permit", required=True)

    s = sub.add_parser("permit", help="查看许可")
    s.add_argument("--permit", required=True)

    sub.add_parser("permits", help="列出全部许可")

    s = sub.add_parser("audit", help="查询审计记录")
    s.add_argument("--entity-type", default=None)
    s.add_argument("--entity-id", default=None)

    sub.add_parser("audit-verify", help="校验审计哈希链完整性")

    s = sub.add_parser("serve", help="启动 REST 接口服务")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)

    sub.add_parser("demo", help="运行端到端演示（冲突、抢修、恢复、审计）")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    actor = {
        "name": args.actor,
        "roles": [r for r in args.roles.split(",") if r],
        "emergency_level": args.emergency_level,
    }
    if args.cmd == "serve":
        from .api import serve
        serve(args.db, args.host, args.port)
        return 0

    service = GalleryService(args.db)
    try:
        cmd = args.cmd
        if cmd == "add-section":
            _print(service.add_section(actor=actor, section=_load_file(args.file)))
        elif cmd == "sections":
            _print({"sections": service.list_sections()})
        elif cmd == "occupancy":
            _print(service.section_occupancy(args.section))
        elif cmd == "submit":
            body = _load_file(args.file)
            _print(service.submit_plan(
                actor=actor, occupancies=body.get("occupancies", []),
                depends_on=body.get("depends_on", []),
                idempotency_key=args.idempotency_key))
        elif cmd == "amend":
            body = _load_file(args.file)
            _print(service.amend_plan(
                actor=actor, plan_id=args.plan, occupancies=body.get("occupancies", []),
                depends_on=body.get("depends_on"), idempotency_key=args.idempotency_key))
        elif cmd == "plan":
            _print(service.get_plan(args.plan, args.version))
        elif cmd == "conflicts":
            _print(service.plan_conflicts(args.plan, args.version))
        elif cmd == "tech-review":
            _print(service.tech_review(
                actor=actor, plan_id=args.plan, version=args.version,
                approve=not args.reject, comment=args.comment))
        elif cmd == "ops-approve":
            _print(service.ops_approve(
                actor=actor, plan_id=args.plan, version=args.version,
                valid_hours=args.valid_hours))
        elif cmd == "check-in":
            _print(service.check_in(actor=actor, permit_id=args.permit))
        elif cmd == "acceptance-request":
            _print(service.request_acceptance(actor=actor, permit_id=args.permit))
        elif cmd == "acceptance":
            _print(service.complete_acceptance(
                actor=actor, permit_id=args.permit, passed=not args.fail,
                comment=args.comment))
        elif cmd == "emergency":
            body = _load_file(args.file)
            _print(service.emergency_occupy(
                actor=actor, occupancies=body.get("occupancies", []),
                reason=body.get("reason", ""), idempotency_key=args.idempotency_key))
        elif cmd == "recovery-plan":
            _print(service.recovery_plan(args.permit))
        elif cmd == "permit":
            _print(service.get_permit(args.permit))
        elif cmd == "permits":
            _print({"permits": service.list_permits()})
        elif cmd == "audit":
            _print({"audit": service.audit_trail(args.entity_type, args.entity_id)})
        elif cmd == "audit-verify":
            _print(service.verify_audit())
        elif cmd == "demo":
            _print({"steps": run_demo(service)})
        return 0
    except ServiceError as exc:
        _print({"error": exc.to_dict()})
        return 2


if __name__ == "__main__":
    sys.exit(main())
