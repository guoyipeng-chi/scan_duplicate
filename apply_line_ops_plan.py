from __future__ import annotations

import argparse
import json
from pathlib import Path

from deduper.config import load_config
from deduper.line_ops_executor import apply_line_ops_plan
from deduper.types import LLMLineOpsPlan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply structured line-ops refactor plan JSON")
    parser.add_argument("--plan", required=True, help="line-ops plan JSON path")
    parser.add_argument("--config", required=False, help="deduper config path")
    parser.add_argument("--apply", action="store_true", help="write changes to files")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(Path(args.config) if args.config else None)
    payload = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    plan = LLMLineOpsPlan.from_dict(payload)
    logs = apply_line_ops_plan(config.workspace, plan, dry_run=not args.apply)
    print("\n".join(logs))
    print("已应用修改" if args.apply else "dry-run 预演完成（未写入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
