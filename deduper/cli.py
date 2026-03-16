from __future__ import annotations

import argparse
import difflib
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
from .line_ops_executor import apply_line_ops_plan
from .llm_clients import (
    generate_line_ops_plan,
    generate_refactor_plan,
    line_ops_plan_to_pretty_json,
    plan_to_pretty_json,
)
from .types import DuplicationGroup, LLMLineOpsPlan, LLMRefactorPlan


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


def _render_occurrence_snippet(workspace: Path, group: DuplicationGroup, occurrence) -> tuple[str, int, int, str, int]:
    full_path = _resolve_occurrence_path(workspace, occurrence.path)
    rel = _to_rel_display(full_path, workspace)
    if not full_path.exists():
        return rel, 0, 0, "<file not found>", 0

    content = full_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    total = len(lines)
    start_line = max(1, occurrence.line)
    if occurrence.end_line is not None:
        end_line = min(total, max(start_line, occurrence.end_line))
    else:
        end_line = min(total, start_line + max(1, group.lines) - 1)

    snippet_lines = [f"{line_no}|{lines[line_no - 1]}" for line_no in range(start_line, end_line + 1)]
    snippet = "\n".join(snippet_lines) if snippet_lines else "<empty>"
    return rel, start_line, end_line, snippet, total


def _extract_occurrence_raw_snippet(workspace: Path, group: DuplicationGroup, occurrence) -> str | None:
    full_path = _resolve_occurrence_path(workspace, occurrence.path)
    if not full_path.exists():
        return None

    lines = full_path.read_text(encoding="utf-8").splitlines()
    total = len(lines)
    start_line = max(1, occurrence.line)
    if occurrence.end_line is not None:
        end_line = min(total, max(start_line, occurrence.end_line))
    else:
        end_line = min(total, start_line + max(1, group.lines) - 1)
    if start_line > total:
        return ""
    return "\n".join(lines[start_line - 1 : end_line])


def _is_group_exact_duplicate(group: DuplicationGroup, workspace: Path) -> bool:
    snippets: list[str] = []
    for occurrence in group.occurrences:
        snippet = _extract_occurrence_raw_snippet(workspace, group, occurrence)
        if snippet is None:
            return False
        snippets.append(snippet.strip())

    if len(snippets) <= 1:
        return True
    first = snippets[0]
    return all(item == first for item in snippets[1:])


def _filter_groups_by_mode(
    groups: list[DuplicationGroup],
    filter_mode: str,
    exact_cache: dict[int, bool],
) -> list[DuplicationGroup]:
    if filter_mode == "exact":
        return [item for item in groups if exact_cache.get(item.id, False)]
    return groups


def _render_context_window(
    workspace: Path,
    group: DuplicationGroup,
    occurrence,
    window: int = 5,
) -> tuple[str, int, int, str, str, str, int]:
    full_path = _resolve_occurrence_path(workspace, occurrence.path)
    rel = _to_rel_display(full_path, workspace)
    if not full_path.exists():
        return rel, 0, 0, "<empty>", "<file not found>", "<empty>", 0

    content = full_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    total = len(lines)
    start_line = max(1, occurrence.line)
    if occurrence.end_line is not None:
        end_line = min(total, max(start_line, occurrence.end_line))
    else:
        end_line = min(total, start_line + max(1, group.lines) - 1)

    before_start = max(1, start_line - window)
    after_end = min(total, end_line + window)

    before = "\n".join(
        f"{line_no}|{lines[line_no - 1]}" for line_no in range(before_start, start_line)
    ) or "<empty>"
    selected = "\n".join(
        f"{line_no}|{lines[line_no - 1]}" for line_no in range(start_line, end_line + 1)
    ) or "<empty>"
    after = "\n".join(
        f"{line_no}|{lines[line_no - 1]}" for line_no in range(end_line + 1, after_end + 1)
    ) or "<empty>"
    return rel, start_line, end_line, before, selected, after, total


def _normalize_code_for_diff(code: str) -> list[str]:
    return [line.strip() for line in code.splitlines()]


def _strip_line_prefix(code: str) -> str:
    items: list[str] = []
    for line in code.splitlines():
        items.append(re.sub(r"^\s*\d+\|", "", line))
    return "\n".join(items)


def _extract_unified_diff_excerpt(canonical_code: str, occurrence_code: str, limit: int = 40) -> list[str]:
    canonical_lines = _normalize_code_for_diff(canonical_code)
    occurrence_lines = _normalize_code_for_diff(occurrence_code)
    diff_lines = list(
        difflib.unified_diff(
            canonical_lines,
            occurrence_lines,
            fromfile="canonical",
            tofile="occurrence",
            lineterm="",
            n=1,
        )
    )
    if not diff_lines:
        return ["<no diff>"]
    return diff_lines[:limit]


def _summarize_occurrence_diff(canonical_code: str, occurrence_code: str) -> list[str]:
    canonical_lines = _normalize_code_for_diff(canonical_code)
    occurrence_lines = _normalize_code_for_diff(occurrence_code)
    matcher = difflib.SequenceMatcher(None, canonical_lines, occurrence_lines)

    summaries: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        canonical_chunk = [line for line in canonical_lines[i1:i2] if line]
        occurrence_chunk = [line for line in occurrence_lines[j1:j2] if line]
        if tag == "replace":
            summaries.append(
                f"replace canonical[{i1 + 1}:{i2}] -> occurrence[{j1 + 1}:{j2}]: "
                f"{' | '.join(canonical_chunk[:2]) or '<empty>'} => {' | '.join(occurrence_chunk[:2]) or '<empty>'}"
            )
        elif tag == "delete":
            summaries.append(
                f"delete canonical[{i1 + 1}:{i2}]: {' | '.join(canonical_chunk[:2]) or '<empty>'}"
            )
        elif tag == "insert":
            summaries.append(
                f"insert occurrence[{j1 + 1}:{j2}]: {' | '.join(occurrence_chunk[:2]) or '<empty>'}"
            )
        if len(summaries) >= 6:
            break

    return summaries or ["与 canonical 片段相比无结构性差异，主要可能是命名或格式差异。"]


def _build_claude_input_markdown(groups: list[DuplicationGroup], workspace: Path) -> str:
    sections: list[str] = []
    sections.append("# Claude Refactor Input")
    sections.append("")
    sections.append("## Goal")
    sections.append("- 消除所选重复片段，提取公共实现，尽量保持行为一致。")
    sections.append("- 只允许修改下面列出的相关文件。")
    sections.append("")
    sections.append("## Refactor Strategy")
    sections.append("- 优先将重复逻辑提取到公共头文件（例如 `common_*.h`），并保证可被相关源文件复用。")
    sections.append("- 若不同调用点存在细节差异，不要复制粘贴多套实现；请在公共实现中通过各自宏进行区分。")
    sections.append("- 宏命名要求语义清晰、作用域最小化，避免污染全局；必要时在调用文件内定义并在使用后取消定义。")
    sections.append("- 保持行为兼容：重构后输入输出语义不变，边界条件处理不变。")
    sections.append("- 优先根据 canonical 片段抽取公共部分；不要在每个 occurrence 上重复实现同一逻辑。")
    sections.append("")
    sections.append("## Output Format (STRICT JSON)")
    sections.append("```json")
    sections.append('{"common_file":"path","common_code":"...","replacements":[{"file":"path","start_line":1,"end_line":1,"replacement":"..."}],"notes":"..."}')
    sections.append("```")
    sections.append("")

    for group in groups:
        sections.append(f"## Group {group.id}")
        sections.append(f"- lines: {group.lines}")
        sections.append(f"- tokens: {group.tokens}")
        sections.append(f"- occurrences: {len(group.occurrences)}")
        involved_files = sorted({_to_rel_display(_resolve_occurrence_path(workspace, occ.path), workspace) for occ in group.occurrences})
        sections.append(f"- involved_files: {', '.join(involved_files)}")
        sections.append("")

        if group.code_fragment:
            sections.append("### Canonical Duplicate Fragment")
            sections.append("```c")
            sections.append(group.code_fragment)
            sections.append("```")
            sections.append("")

        for idx, occurrence in enumerate(group.occurrences, start=1):
            rel, start_line, end_line, before, selected, after, total = _render_context_window(workspace, group, occurrence)
            diff_summary = _summarize_occurrence_diff(group.code_fragment or selected, selected)
            sections.append(f"### Occurrence {idx}")
            sections.append(f"- file: {rel}")
            sections.append(f"- selected_range: {start_line}-{end_line}")
            sections.append(f"- file_total_lines: {total}")
            sections.append("- difference_summary:")
            for item in diff_summary:
                sections.append(f"  - {item}")
            sections.append("")
            sections.append("#### Local Context Before")
            sections.append("```c")
            sections.append(before)
            sections.append("```")
            sections.append("")
            sections.append("#### Selected Range")
            sections.append("```c")
            sections.append(selected)
            sections.append("```")
            sections.append("")
            sections.append("#### Local Context After")
            sections.append("```c")
            sections.append(after)
            sections.append("```")
            sections.append("")

    return "\n".join(sections).strip() + "\n"


def _build_line_ops_input_markdown(groups: list[DuplicationGroup], workspace: Path) -> str:
    sections: list[str] = []
    sections.append("# Line-Ops Refactor Input")
    sections.append("")
    sections.append("## Task Constraints")
    sections.append("- 仅允许输出 line-ops JSON（cut_paste / delete / insert）。")
    sections.append("- 仅允许修改下面列出的文件与行号区间。")
    sections.append("- 不要输出整段替换方案。")
    sections.append("- 重构策略：提取公共头文件，差异通过宏开放。")
    sections.append("")
    sections.append("## Output JSON Schema")
    sections.append("```json")
    sections.append('{"operations":{"cut_paste":[{"source_file":"path","start_line":1,"end_line":1,"target_file":"path","target_line":1,"position":"after"}],"delete":[{"file":"path","start_line":1,"end_line":1}],"insert":[{"file":"path","line":1,"position":"after","content":"..."}]},"notes":"..."}')
    sections.append("```")
    sections.append("")

    for group in groups:
        sections.append(f"## Group {group.id}")
        sections.append(f"- pmd_lines: {group.lines}")
        sections.append(f"- pmd_tokens: {group.tokens}")
        sections.append(f"- occurrences: {len(group.occurrences)}")
        involved_files = sorted({_to_rel_display(_resolve_occurrence_path(workspace, occ.path), workspace) for occ in group.occurrences})
        sections.append(f"- editable_files: {', '.join(involved_files)}")
        sections.append("")

        if group.code_fragment:
            sections.append("### Canonical Fragment")
            sections.append("```c")
            sections.append(group.code_fragment)
            sections.append("```")
            sections.append("")

        for idx, occurrence in enumerate(group.occurrences, start=1):
            rel, start_line, end_line, before, selected, after, total = _render_context_window(workspace, group, occurrence)
            selected_no_prefix = _strip_line_prefix(selected)
            diff_excerpt = _extract_unified_diff_excerpt(group.code_fragment or selected_no_prefix, selected_no_prefix)
            sections.append(f"### Occurrence {idx}")
            sections.append(f"- file: {rel}")
            sections.append(f"- range: {start_line}-{end_line}")
            sections.append(f"- file_total_lines: {total}")
            sections.append("- diff_excerpt:")
            sections.append("```diff")
            sections.extend(diff_excerpt)
            sections.append("```")
            sections.append("- local_before:")
            sections.append("```c")
            sections.append(before)
            sections.append("```")
            sections.append("- local_after:")
            sections.append("```c")
            sections.append(after)
            sections.append("```")
            sections.append("")

    return "\n".join(sections).strip() + "\n"


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


def _collect_line_ops_files(workspace: Path, plan: LLMLineOpsPlan) -> list[Path]:
    files: set[Path] = set()
    for item in plan.cut_paste:
        files.add(_resolve_workspace_file(workspace, item.source_file))
        files.add(_resolve_workspace_file(workspace, item.target_file))
    for item in plan.delete:
        files.add(_resolve_workspace_file(workspace, item.file))
    for item in plan.insert:
        files.add(_resolve_workspace_file(workspace, item.file))
    return sorted(files)


def _apply_line_ops_with_guards(
    config,
    plan: LLMLineOpsPlan,
    args: argparse.Namespace,
    branch_prefix: str,
    create_branch: bool = True,
) -> list[str]:
    _run_build_guards(config, "before_apply")
    apply_line_ops_plan(config.workspace, plan, dry_run=True)

    tracked_files = _collect_line_ops_files(config.workspace, plan)
    snapshot = _snapshot_files(tracked_files)
    if create_branch:
        _maybe_create_branch(config.workspace, args, prefix=branch_prefix)

    try:
        logs = apply_line_ops_plan(config.workspace, plan, dry_run=False)
        _run_build_guards(config, "after_apply")
        return logs
    except Exception as exc:
        _restore_snapshot(snapshot)
        raise RuntimeError(f"line-ops 重构后编译/应用失败，已回滚文件变更: {exc}") from exc


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

    exact_cache = {item.id: _is_group_exact_duplicate(item, workspace) for item in groups}
    filter_mode = "exact"
    page = 1
    while True:
        filtered = _filter_groups_by_mode(groups, filter_mode, exact_cache)
        if not filtered:
            print(f"当前筛选模式 {filter_mode} 下无可选重复组。")
            filter_mode = "all"
            continue

        total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        exact_count = sum(1 for flag in exact_cache.values() if flag)
        print(f"筛选模式: {filter_mode}（完全雷同组 {exact_count}/{len(groups)}）")
        _print_table_page(filtered, workspace, page, page_size)
        raw = input("输入 n/p 翻页，输入 f 切换筛选（exact/all），直接输入 ID 开始重构，输入 q 退出: ").strip()
        lowered = raw.lower()
        if lowered == "q":
            return None
        if lowered == "f":
            filter_mode = "all" if filter_mode == "exact" else "exact"
            page = 1
            continue
        if lowered == "n":
            page = min(total_pages, page + 1)
            continue
        if lowered == "p":
            page = max(1, page - 1)
            continue
        selected_ids = _parse_selection_input(raw)
        if selected_ids:
            visible_ids = {item.id for item in filtered}
            picked = {item for item in selected_ids if item in visible_ids}
            if not picked:
                print("当前筛选结果中不包含所选 ID，请先切换筛选或翻页查看。")
                continue
            return picked
        if lowered.startswith("s"):
            print("请选择组 ID，例如: 1 或 1,3")
            continue
        print("无效输入，请重试。")


def _preview_with_paging(groups: list[DuplicationGroup], workspace: Path, page_size: int) -> set[int] | None:
    if not groups:
        print("重复组为空。")
        return None
    exact_cache = {item.id: _is_group_exact_duplicate(item, workspace) for item in groups}
    filter_mode = "exact"
    page = 1
    while True:
        filtered = _filter_groups_by_mode(groups, filter_mode, exact_cache)
        if not filtered:
            print(f"当前筛选模式 {filter_mode} 下无可选重复组。")
            filter_mode = "all"
            continue

        total_pages = max(1, (len(filtered) + page_size - 1) // page_size)
        page = max(1, min(page, total_pages))
        exact_count = sum(1 for flag in exact_cache.values() if flag)
        print(f"筛选模式: {filter_mode}（完全雷同组 {exact_count}/{len(groups)}）")
        _print_table_page(filtered, workspace, page, page_size)
        raw = input("输入 n/p 翻页，输入 f 切换筛选（exact/all），直接输入 ID 开始重构，输入 q 退出: ").strip()
        lowered = raw.lower()
        if lowered == "q":
            return None
        if lowered == "f":
            filter_mode = "all" if filter_mode == "exact" else "exact"
            page = 1
            continue
        if lowered == "n":
            page = min(total_pages, page + 1)
            continue
        if lowered == "p":
            page = max(1, page - 1)
            continue
        selected_ids = _parse_selection_input(raw)
        if selected_ids:
            visible_ids = {item.id for item in filtered}
            picked = {item for item in selected_ids if item in visible_ids}
            if not picked:
                print("当前筛选结果中不包含所选 ID，请先切换筛选或翻页查看。")
                continue
            return picked
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

    plan_mode = getattr(args, "plan_mode", "replacement")

    if plan_mode == "line-ops":
        line_ops_markdown = _build_line_ops_input_markdown(selected_groups, config.workspace)
        line_ops_markdown_file = Path(getattr(args, "out_claude_markdown", "artifacts/claude_refactor_input.md"))
        line_ops_markdown_file.parent.mkdir(parents=True, exist_ok=True)
        line_ops_markdown_file.write_text(line_ops_markdown, encoding="utf-8")
        print(f"已生成 Line-Ops 输入文件: {line_ops_markdown_file}")

        line_ops_plan: LLMLineOpsPlan | None = None
        last_error: Exception | None = None
        feedback: str | None = None
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            line_ops_plan = generate_line_ops_plan(config.model, line_ops_markdown, feedback=feedback)
            try:
                apply_line_ops_plan(config.workspace, line_ops_plan, dry_run=True)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                feedback = str(exc)
                print(f"[line-ops] 计划校验失败，第 {attempt}/{max_attempts} 次: {exc}")

        if line_ops_plan is None or last_error is not None:
            raise RuntimeError(f"line-ops 计划生成失败，超过重试上限: {last_error}")

        plan_file = Path(getattr(args, "out_plan", "artifacts/refactor_plan.json"))
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(line_ops_plan_to_pretty_json(line_ops_plan), encoding="utf-8")
        print(f"已生成 line-ops 计划: {plan_file}")

        if getattr(args, "apply", False):
            logs = _apply_line_ops_with_guards(
                config,
                line_ops_plan,
                args,
                branch_prefix="deduper/refactor-line-ops",
                create_branch=create_branch,
            )
        else:
            logs = apply_line_ops_plan(config.workspace, line_ops_plan, dry_run=True)
        print("\n".join(logs))
        print("已应用修改" if getattr(args, "apply", False) else "dry-run 预演完成（未写入）")
        return 0

    context = _collect_context(selected_groups, config.workspace)
    if not context:
        raise ValueError("未加载到任何文件上下文，请检查 workspace 与 CPD path")

    claude_markdown_file = Path(getattr(args, "out_claude_markdown", "artifacts/claude_refactor_input.md"))
    claude_markdown_file.parent.mkdir(parents=True, exist_ok=True)
    claude_markdown = _build_claude_input_markdown(selected_groups, config.workspace)
    claude_markdown_file.write_text(claude_markdown, encoding="utf-8")
    print(f"已生成 Claude 输入文件: {claude_markdown_file}")

    plan = generate_refactor_plan(
        config.model,
        selected_groups,
        context,
        agent_markdown=claude_markdown,
    )
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

    if getattr(args, "plan_mode", "replacement") == "line-ops":
        line_ops_plan = LLMLineOpsPlan.from_dict(payload)
        if args.apply:
            logs = _apply_line_ops_with_guards(config, line_ops_plan, args, branch_prefix="deduper/apply-line-ops")
        else:
            logs = apply_line_ops_plan(config.workspace, line_ops_plan, dry_run=True)
        print("\n".join(logs))
        print("已应用修改" if args.apply else "dry-run 预演完成（未写入）")
        return 0

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
        description="基于 PMD CPD XML + 大模型（Claude/vLLM/Ollama）的重复代码提取工具",
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
    p_list.add_argument(
        "--out-claude-markdown",
        default="artifacts/claude_refactor_input.md",
        help="直接从预览进入重构时输出给 Claude 的输入 Markdown 路径",
    )
    p_list.add_argument(
        "--plan-mode",
        choices=["replacement", "line-ops"],
        default="replacement",
        help="计划模式：replacement=整段替换；line-ops=结构化行操作",
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
    p_refactor.add_argument(
        "--out-claude-markdown",
        default="artifacts/claude_refactor_input.md",
        help="输出给 Claude 的输入 Markdown 路径",
    )
    p_refactor.add_argument(
        "--plan-mode",
        choices=["replacement", "line-ops"],
        default="replacement",
        help="计划模式：replacement=整段替换；line-ops=结构化行操作",
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
    p_apply.add_argument(
        "--plan-mode",
        choices=["replacement", "line-ops"],
        default="replacement",
        help="计划模式：replacement=整段替换；line-ops=结构化行操作",
    )
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
    p_workflow.add_argument(
        "--out-claude-markdown",
        default="artifacts/claude_refactor_input.md",
        help="full 模式下输出给 Claude 的输入 Markdown 路径",
    )
    p_workflow.add_argument(
        "--plan-mode",
        choices=["replacement", "line-ops"],
        default="replacement",
        help="计划模式：replacement=整段替换；line-ops=结构化行操作",
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
