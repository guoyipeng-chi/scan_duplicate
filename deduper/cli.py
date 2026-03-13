from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
from typing import Iterable

from .apply_changes import apply_refactor_plan
from .build_utils import BuildFailedError, run_build_step
from .config import load_config
from .cpd_parser import parse_cpd_xml
from .git_utils import GitRepoError, create_branch_from_current, is_git_repo
from .llm_clients import generate_refactor_plan, plan_to_pretty_json
from .types import DuplicationGroup, LLMRefactorPlan


def _resolve_occurrence_path(workspace: Path, raw: Path) -> Path:
    if raw.is_absolute():
        return raw
    return (workspace / raw).resolve()


def _collect_context(groups: list[DuplicationGroup], workspace: Path) -> dict[str, str]:
    contexts: dict[str, str] = {}
    files = {
        occ.path
        for group in groups
        for occ in group.occurrences
    }
    for original_path in files:
        full = _resolve_occurrence_path(workspace, original_path)
        if not full.exists():
            continue
        key = str(original_path)
        contexts[key] = full.read_text(encoding="utf-8")
    return contexts


def _parse_group_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    normalized = raw.replace("，", ",")
    for part in re.split(r"[,\s]+", normalized):
        part = part.strip()
        if not part:
            continue
        ids.add(int(part))
    return ids


def _parse_selection_input(raw: str) -> set[int] | None:
    value = raw.strip()
    if not value:
        return None

    lowered = value.lower()
    if lowered.startswith("s"):
        payload = value[1:].strip()
        return _parse_group_ids(payload) if payload else None

    if re.fullmatch(r"\d+(?:[\s,，]+\d+)*", value):
        return _parse_group_ids(value)

    return None


def _resolve_workspace_file(workspace: Path, raw: Path) -> Path:
    if raw.is_absolute():
        return raw.resolve()
    return (workspace / raw).resolve()


def _load_config_with_fallback(config_path: Path | None, repo_path: str | None = None):
    explicit = config_path.resolve() if config_path else None
    if explicit is not None:
        return load_config(explicit)

    default_path = Path("deduper.config.json")
    if default_path.exists():
        return load_config(default_path)

    demo_config = Path(__file__).resolve().parents[1] / "demo_assets" / "deduper.demo.config.json"
    if demo_config.exists():
        print(f"未检测到 deduper.config.json，已自动回退到演示配置: {demo_config}")
        return load_config(demo_config)

    example_config = Path(__file__).resolve().parents[1] / "deduper.config.example.json"
    if example_config.exists():
        print(f"未检测到 deduper.config.json，已临时使用示例配置: {example_config}")
        return load_config(example_config)

    raise ValueError(
        "缺少可用配置文件。请传 --config，或先复制 deduper.config.example.json 为 deduper.config.json 并填写模型参数。"
    )


def _collect_plan_files(workspace: Path, plan: LLMRefactorPlan) -> list[Path]:
    files = {_resolve_workspace_file(workspace, plan.common_file)}
    for item in plan.replacements:
        files.add(_resolve_workspace_file(workspace, item.file))
    return sorted(files)


def _snapshot_files(paths: Iterable[Path]) -> dict[Path, str | None]:
    snapshot: dict[Path, str | None] = {}
    for path in paths:
        snapshot[path] = path.read_text(encoding="utf-8") if path.exists() else None
    return snapshot


def _restore_snapshot(snapshot: dict[Path, str | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _run_build_guards(config, stage: str) -> None:
    if stage == "before_apply":
        run_build_step(config.workspace, config.build.before_apply, stage)
    elif stage == "after_apply":
        run_build_step(config.workspace, config.build.after_apply, stage)


def _apply_with_guards(
    config,
    plan: LLMRefactorPlan,
    args: argparse.Namespace,
    branch_prefix: str,
    create_branch: bool = True,
) -> list[str]:
    _run_build_guards(config, "before_apply")
    apply_refactor_plan(config.workspace, plan, dry_run=True)

    tracked_files = _collect_plan_files(config.workspace, plan)
    snapshot = _snapshot_files(tracked_files)
    if create_branch:
        _maybe_create_branch(config.workspace, args, prefix=branch_prefix)

    try:
        logs = apply_refactor_plan(config.workspace, plan, dry_run=False)
        _run_build_guards(config, "after_apply")
        return logs
    except Exception as exc:
        _restore_snapshot(snapshot)
        raise RuntimeError(f"重构后编译/应用失败，已回滚文件变更: {exc}") from exc


def _maybe_create_branch(workspace: Path, args: argparse.Namespace, prefix: str) -> bool:
    if not getattr(args, "apply", False):
        return False
    if not getattr(args, "git_branch", True):
        return False
    if not is_git_repo(workspace):
        print(f"[git] 跳过建分支：不是 Git 仓库 {workspace}")
        return False

    preferred = args.git_branch_name or prefix
    try:
        branch = create_branch_from_current(workspace, preferred_name=preferred)
        print(f"[git] 已从当前节点创建并切换到新分支: {branch}")
        return True
    except GitRepoError as exc:
        raise RuntimeError(f"创建 Git 分支失败: {exc}") from exc


def _run_scan_workflow(args: argparse.Namespace) -> Path:
    script = Path(__file__).resolve().parents[1] / "scan_c_duplication.py"
    repo = Path(args.repo).resolve()
    out_dir = Path(args.out_dir).resolve()

    command = [
        sys.executable,
        str(script),
        str(repo),
        "--out-dir",
        str(out_dir),
        "--min-tokens",
        str(args.min_tokens),
        "--language",
        args.language,
    ]
    if args.pmd:
        command.extend(["--pmd", args.pmd])
    if not args.auto_install_pmd:
        command.append("--no-auto-install-pmd")
    if not args.auto_install_java:
        command.append("--no-auto-install-java")
    if not args.ignore_identifiers:
        command.append("--no-ignore-identifiers")
    if not args.ignore_literals:
        command.append("--no-ignore-literals")

    subprocess.run(command, check=True)

    report_dirs = sorted(out_dir.glob(f"cpd_report_{repo.name}_*"))
    if not report_dirs:
        raise FileNotFoundError(f"未找到扫描产物目录: {out_dir}")
    xml_path = report_dirs[-1] / "duplication.xml"
    if not xml_path.exists():
        raise FileNotFoundError(f"未找到 duplication.xml: {xml_path}")
    return xml_path


def _to_rel_display(path: Path, workspace: Path) -> str:
    resolved = path.resolve()
    workspace_resolved = workspace.resolve()
    try:
        return str(resolved.relative_to(workspace_resolved)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _format_location_lines(group: DuplicationGroup, workspace: Path) -> list[str]:
    parts: list[str] = []
    for occ in group.occurrences:
        full = _resolve_occurrence_path(workspace, occ.path)
        rel = _to_rel_display(full, workspace)
        start = f"{occ.line}:{occ.column or 1}"
        if occ.end_line is not None and occ.end_column is not None:
            end = f"-{occ.end_line}:{occ.end_column}"
        else:
            end = ""
        parts.append(f"{rel}@{start}{end}")
    return parts


def _sorted_groups(groups: list[DuplicationGroup]) -> list[DuplicationGroup]:
    return sorted(
        groups,
        key=lambda group: (len(group.occurrences) * group.lines, group.tokens, group.id),
        reverse=True,
    )


def _print_table_page(groups: list[DuplicationGroup], workspace: Path, page: int, page_size: int) -> None:
    ordered = _sorted_groups(groups)
    total_pages = max(1, (len(ordered) + page_size - 1) // page_size)
    current = max(1, min(page, total_pages))
    start = (current - 1) * page_size
    subset = ordered[start : start + page_size]

    print(f"\n重复组预览（按 重复数×重复行数 降序） 第 {current}/{total_pages} 页")

    term_width = shutil.get_terminal_size((96, 40)).columns
    id_w = 3
    cnt_w = 3
    lines_w = 5
    total_w = 6
    fixed = id_w + cnt_w + lines_w + total_w + 6 * 3
    loc_w = min(36, max(24, term_width - fixed))

    headers = [
        ("ID", id_w),
        ("Cnt", cnt_w),
        ("Lines", lines_w),
        ("DupLn", total_w),
        ("Locations", loc_w),
    ]

    def _border() -> str:
        return "+" + "+".join("-" * (width + 2) for _, width in headers) + "+"

    def _pad(value: str, width: int) -> str:
        text = value[:width]
        return text + " " * max(0, width - len(text))

    def _wrap_cell_lines(lines: list[str], width: int) -> list[str]:
        wrapped: list[str] = []
        for line in lines:
            chunks = textwrap.wrap(line, width=width, break_long_words=True, break_on_hyphens=False)
            if chunks:
                wrapped.extend(chunks)
            else:
                wrapped.append("")
        return wrapped or [""]

    print(_border())
    print(
        "| "
        + " | ".join(_pad(title, width) for title, width in headers)
        + " |"
    )
    print(_border())

    for group in subset:
        count = len(group.occurrences)
        total_dup = count * group.lines
        location_lines = _format_location_lines(group, workspace)

        row_cells = [
            _wrap_cell_lines([str(group.id)], id_w),
            _wrap_cell_lines([str(count)], cnt_w),
            _wrap_cell_lines([str(group.lines)], lines_w),
            _wrap_cell_lines([str(total_dup)], total_w),
            _wrap_cell_lines(location_lines, loc_w),
        ]
        row_height = max(len(cell) for cell in row_cells)

        for i in range(row_height):
            values = []
            for (title, width), cell in zip(headers, row_cells):
                content = cell[i] if i < len(cell) else ""
                values.append(_pad(content, width))
            print("| " + " | ".join(values) + " |")
        print(_border())


def _choose_groups_with_paging(groups: list[DuplicationGroup], workspace: Path, page_size: int) -> set[int] | None:
    if not groups:
        print("重复组为空。")
        return None

    page = 1
    total_pages = max(1, (len(groups) + page_size - 1) // page_size)
    while True:
        _print_table_page(groups, workspace, page, page_size)
        raw = input("输入 n/p 翻页，直接输入 ID 数字（如 1 或 1,2）开始重构，输入 q 退出: ").strip()
        if raw.lower() == "q":
            return None
        if raw.lower() == "n":
            page = min(total_pages, page + 1)
            continue
        if raw.lower() == "p":
            page = max(1, page - 1)
            continue
        selected_ids = _parse_selection_input(raw)
        if selected_ids:
            return selected_ids
        if raw.lower().startswith("s"):
            print("请选择组 ID，例如: 1 或 1,3")
            continue
        print("无效输入，请重试。")


def _preview_with_paging(groups: list[DuplicationGroup], workspace: Path, page_size: int) -> set[int] | None:
    if not groups:
        print("重复组为空。")
        return None
    page = 1
    total_pages = max(1, (len(groups) + page_size - 1) // page_size)
    while True:
        _print_table_page(groups, workspace, page, page_size)
        raw = input("输入 n/p 翻页，直接输入 ID 数字（如 1 或 1,2）开始重构，输入 q 退出: ").strip()
        lowered = raw.lower()
        if lowered == "q":
            return None
        if lowered == "n":
            page = min(total_pages, page + 1)
            continue
        if lowered == "p":
            page = max(1, page - 1)
            continue
        selected_ids = _parse_selection_input(raw)
        if selected_ids:
            return selected_ids
        print("无效输入，请重试。")


def _run_refactor_for_selected_groups(
    config,
    groups: list[DuplicationGroup],
    selected_ids: set[int],
    args: argparse.Namespace,
    *,
    create_branch: bool,
) -> int:
    selected_groups = [item for item in groups if item.id in selected_ids]
    if not selected_groups:
        raise ValueError("未匹配到任何 group id，请检查输入的 ID")

    context = _collect_context(selected_groups, config.workspace)
    if not context:
        raise ValueError("未加载到任何文件上下文，请检查 workspace 与 CPD path")

    plan = generate_refactor_plan(config.model, selected_groups, context)
    plan_file = Path(getattr(args, "out_plan", "artifacts/refactor_plan.json"))
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(plan_to_pretty_json(plan), encoding="utf-8")
    print(f"已生成计划: {plan_file}")

    if getattr(args, "apply", False):
        logs = _apply_with_guards(
            config,
            plan,
            args,
            branch_prefix="deduper/refactor",
            create_branch=create_branch,
        )
    else:
        logs = apply_refactor_plan(config.workspace, plan, dry_run=True)
    print("\n".join(logs))
    print("已应用修改" if getattr(args, "apply", False) else "dry-run 预演完成（未写入）")
    return 0


def _run_refactor_from_xml(config_path: Path | None, xml_path: Path, args: argparse.Namespace) -> int:
    config = _load_config_with_fallback(config_path, getattr(args, "repo", None))
    groups = parse_cpd_xml(xml_path)
    selected_ids: set[int]
    if args.interactive:
        selected = _choose_groups_with_paging(
            groups,
            config.workspace,
            page_size=getattr(args, "page_size", 8),
        )
        if not selected:
            raise ValueError("用户取消选择，未执行重构")
        selected_ids = selected
    else:
        if not args.groups:
            raise ValueError("非交互模式必须传 --groups")
        selected_ids = _parse_group_ids(args.groups)

    return _run_refactor_for_selected_groups(
        config,
        groups,
        selected_ids,
        args,
        create_branch=True,
    )


def cmd_list(args: argparse.Namespace) -> int:
    groups = parse_cpd_xml(Path(args.xml))
    print(f"发现重复组: {len(groups)}")
    workspace = Path(args.repo).resolve() if getattr(args, "repo", None) else Path.cwd()
    _print_table_page(groups, workspace, page=1, page_size=args.page_size)
    if args.preview:
        selected_ids = _preview_with_paging(groups, workspace, page_size=args.page_size)
        if selected_ids:
            config = _load_config_with_fallback(
                Path(args.config) if args.config else None,
                getattr(args, "repo", None),
            )
            return _run_refactor_for_selected_groups(
                config,
                groups,
                selected_ids,
                args,
                create_branch=True,
            )
    return 0


def cmd_refactor(args: argparse.Namespace) -> int:
    return _run_refactor_from_xml(
        Path(args.config) if args.config else None,
        Path(args.xml),
        args,
    )


def cmd_apply_plan(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config) if args.config else None)
    payload = json.loads(Path(args.plan).read_text(encoding="utf-8"))

    plan = LLMRefactorPlan.from_dict(payload)
    if args.apply:
        logs = _apply_with_guards(config, plan, args, branch_prefix="deduper/apply-plan")
    else:
        logs = apply_refactor_plan(config.workspace, plan, dry_run=True)
    print("\n".join(logs))
    print("已应用修改" if args.apply else "dry-run 预演完成（未写入）")
    return 0


def cmd_workflow(args: argparse.Namespace) -> int:
    xml_path = _run_scan_workflow(args)
    print(f"已生成 XML: {xml_path}")

    config_path = Path(args.config) if args.config else None
    config = _load_config_with_fallback(config_path, args.repo) if config_path or args.mode == "full" else None

    groups = parse_cpd_xml(xml_path)
    workspace = config.workspace if config else Path(args.repo).resolve()

    if args.mode == "scan-only":
        selected_ids = _preview_with_paging(groups, workspace, page_size=args.page_size)
        if not selected_ids:
            return 0
        config = config or _load_config_with_fallback(config_path, args.repo)
        return _run_refactor_for_selected_groups(
            config,
            groups,
            selected_ids,
            args,
            create_branch=True,
        )

    if config is None:
        raise ValueError("full 模式必须提供 --config")

    branch_created_in_session = False
    round_index = 0

    while True:
        round_index += 1
        groups = parse_cpd_xml(xml_path)
        if not groups:
            print("重复组已清空，流程结束。")
            return 0

        selected_ids: set[int]
        if args.interactive or not args.groups or round_index > 1:
            selected = _choose_groups_with_paging(groups, config.workspace, page_size=args.page_size)
            if not selected:
                print("用户取消选择，流程结束。")
                return 0
            selected_ids = selected
        else:
            selected_ids = _parse_group_ids(args.groups)

        _run_refactor_for_selected_groups(
            config,
            groups,
            selected_ids,
            args,
            create_branch=not branch_created_in_session,
        )
        if args.apply:
            if args.git_branch and is_git_repo(config.workspace):
                branch_created_in_session = True

        if not args.apply:
            return 0

        xml_path = _run_scan_workflow(args)
        print(f"重扫完成，新 XML: {xml_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deduper",
        description="基于 PMD CPD XML + 大模型（vLLM/Ollama）的重复代码提取工具",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出 CPD XML 中的重复组")
    p_list.add_argument("--xml", required=True, help="CPD XML 文件路径")
    p_list.add_argument("--repo", required=False, help="可选：项目根目录（用于相对路径展示）")
    p_list.add_argument("--page-size", type=int, default=8, help="表格分页大小")
    p_list.add_argument("--preview", action="store_true", help="开启交互式翻页预览")
    p_list.add_argument("--config", required=False, help="可选：直接从预览进入重构时使用的配置文件路径")
    p_list.add_argument(
        "--out-plan",
        default="artifacts/refactor_plan.json",
        help="直接从预览进入重构时输出计划 JSON 路径",
    )
    p_list.add_argument("--apply", action="store_true", help="直接从预览进入重构时是否真正写入文件")
    p_list.add_argument(
        "--git-branch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="直接从预览进入重构时，真正写入前是否创建新分支（默认开启）",
    )
    p_list.add_argument(
        "--git-branch-name",
        required=False,
        help="直接从预览进入重构时可选的新分支名",
    )
    p_list.set_defaults(func=cmd_list)

    p_refactor = sub.add_parser("refactor", help="选择重复组并生成/应用重构计划")
    p_refactor.add_argument("--xml", required=True, help="CPD XML 文件路径")
    p_refactor.add_argument("--groups", required=False, help="重复组 ID，逗号分隔，如 1,3,5")
    p_refactor.add_argument(
        "--interactive",
        action="store_true",
        help="交互式选择重复组（与 --groups 二选一）",
    )
    p_refactor.add_argument("--page-size", type=int, default=8, help="交互表格分页大小")
    p_refactor.add_argument("--config", required=False, help="配置文件路径")
    p_refactor.add_argument(
        "--out-plan",
        default="artifacts/refactor_plan.json",
        help="输出计划 JSON 路径",
    )
    p_refactor.add_argument("--apply", action="store_true", help="是否真正写入文件")
    p_refactor.add_argument(
        "--git-branch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="真正写入前是否从当前节点创建新分支（默认开启）",
    )
    p_refactor.add_argument(
        "--git-branch-name",
        required=False,
        help="可选：指定新分支名；未传则自动生成",
    )
    p_refactor.set_defaults(func=cmd_refactor)

    p_apply = sub.add_parser("apply-plan", help="从计划 JSON 应用改动")
    p_apply.add_argument("--plan", required=True, help="计划 JSON 路径")
    p_apply.add_argument("--config", required=False, help="配置文件路径")
    p_apply.add_argument("--apply", action="store_true", help="是否真正写入文件")
    p_apply.add_argument(
        "--git-branch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="真正写入前是否从当前节点创建新分支（默认开启）",
    )
    p_apply.add_argument(
        "--git-branch-name",
        required=False,
        help="可选：指定新分支名；未传则自动生成",
    )
    p_apply.set_defaults(func=cmd_apply_plan)

    p_workflow = sub.add_parser("workflow", help="扫描后可选择仅生成 XML 或继续完整去重流程")
    p_workflow.add_argument("--repo", required=True, help="要扫描的仓库路径")
    p_workflow.add_argument(
        "--mode",
        choices=["scan-only", "full"],
        default="full",
        help="scan-only 只跑 PMD 生成 XML；full 跑完整流程",
    )
    p_workflow.add_argument(
        "--out-dir",
        default="artifacts",
        help="扫描输出目录",
    )
    p_workflow.add_argument("--pmd", required=False, help="PMD 可执行文件路径")
    p_workflow.add_argument(
        "--auto-install-pmd",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="PMD 不可用时是否自动安装",
    )
    p_workflow.add_argument(
        "--auto-install-java",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Java 不可用时是否自动安装",
    )
    p_workflow.add_argument("--min-tokens", type=int, default=40, help="CPD 最小 token")
    p_workflow.add_argument(
        "--language",
        default="cpp",
        choices=["cpp", "c"],
        help="CPD 语言",
    )
    p_workflow.add_argument(
        "--ignore-identifiers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否忽略标识符（默认开启）",
    )
    p_workflow.add_argument(
        "--ignore-literals",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否忽略字面量（默认开启）",
    )
    p_workflow.add_argument("--groups", required=False, help="full 模式下要处理的重复组 ID")
    p_workflow.add_argument(
        "--interactive",
        action="store_true",
        help="full 模式下交互式选择重复组",
    )
    p_workflow.add_argument("--config", required=False, help="配置文件路径")
    p_workflow.add_argument(
        "--out-plan",
        default="artifacts/refactor_plan.json",
        help="full 模式下输出计划 JSON 路径",
    )
    p_workflow.add_argument("--apply", action="store_true", help="full 模式下是否真正写入")
    p_workflow.add_argument(
        "--git-branch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="full 模式真正写入前是否建分支",
    )
    p_workflow.add_argument(
        "--git-branch-name",
        required=False,
        help="full 模式可选的新分支名",
    )
    p_workflow.add_argument("--page-size", type=int, default=8, help="预览表格分页大小")
    p_workflow.set_defaults(func=cmd_workflow)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (ValueError, FileNotFoundError, RuntimeError, GitRepoError, BuildFailedError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
