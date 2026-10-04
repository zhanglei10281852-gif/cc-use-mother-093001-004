#!/usr/bin/env python3
"""命令行入口：

  python run_cli.py demo              端到端验证场景（默认）
  python run_cli.py serve [--host H] [--port P]   启动 HTTP 接口
  python run_cli.py topology          打印演示管廊拓扑 JSON
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from tunnel_booking.cli import run_demo  # noqa: E402
from tunnel_booking.demo import build_demo_topology  # noqa: E402


def main(argv: list) -> int:
    cmd = argv[1] if len(argv) > 1 else "demo"
    if cmd == "demo":
        report, code = run_demo()
        print(report)
        return code
    if cmd == "topology":
        print(json.dumps(build_demo_topology().to_dict(), ensure_ascii=False, indent=2))
        return 0
    if cmd == "serve":
        from tunnel_booking.api import main as serve_main
        sys.argv = sys.argv[1:]
        serve_main()
        return 0
    print(f"未知命令: {cmd}；可用：demo | serve | topology", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
