"""显式启动规范化后端的持续调度器。"""

from __future__ import annotations

import argparse

from .backend_scheduler import run_due_once, run_forever


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="只检查并执行一次到期的 continuous 任务")
    parser.add_argument("--interval", type=int, default=30, help="持续调度检查间隔（秒）")
    args = parser.parse_args()
    if args.once:
        print(run_due_once())
        return 0
    print(f"统一后端调度器已启动，检查间隔 {max(1, args.interval)} 秒。按 Ctrl+C 停止。")
    try:
        run_forever(args.interval)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
