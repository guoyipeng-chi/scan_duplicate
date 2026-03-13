from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .types import LLMRefactorPlan, LLMReplacement


def _find_line_by_content(content: str, target_start: int, target_end: int) -> tuple[int, int] | None:
    """尝试根据行号范围找到大致匹配的实际行区间。"""
    lines = content.splitlines(keepends=True)
    total = len(lines)
    
    if target_start >= 1 and target_start <= total and target_end >= target_start and target_end <= total:
        return (target_start, target_end)
    
    if target_end > total:
        target_end = total
    
    if target_start >= 1 and target_start <= total and target_end > target_start:
        return (target_start, target_end)
    
    return None


def _replace_line_range(content: str, start_line: int, end_line: int, replacement: str) -> str:
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)
    
    adjusted_start = start_line
    adjusted_end = end_line
    
    if end_line > total_lines:
        adjusted_end = total_lines
    
    if adjusted_start < 1:
        adjusted_start = 1
    
    if adjusted_start > total_lines or adjusted_end < adjusted_start:
        raise ValueError(
            f"非法行区间: start={start_line}, end={end_line}, file_total={total_lines}"
        )

    replacement_block = replacement
    if replacement_block and not replacement_block.endswith("\n"):
        replacement_block += "\n"

    new_lines = (
        lines[: adjusted_start - 1]
        + replacement_block.splitlines(keepends=True)
        + lines[adjusted_end:]
    )
    return "".join(new_lines)


def apply_refactor_plan(workspace: Path, plan: LLMRefactorPlan, dry_run: bool = True) -> list[str]:
    logs: list[str] = []

    common_file = (workspace / plan.common_file).resolve()
    if not str(common_file).startswith(str(workspace.resolve())):
        raise ValueError(f"common_file 越界: {plan.common_file}")

    grouped: dict[Path, list[LLMReplacement]] = defaultdict(list)
    for item in plan.replacements:
        target = (workspace / item.file).resolve()
        if not str(target).startswith(str(workspace.resolve())):
            raise ValueError(f"replacement file 越界: {item.file}")
        grouped[target].append(item)

    for file_path, replacements in grouped.items():
        if not file_path.exists():
            raise FileNotFoundError(f"待修改文件不存在: {file_path}")

        content = file_path.read_text(encoding="utf-8")
        ordered = sorted(
            replacements,
            key=lambda replacement: (replacement.start_line, replacement.end_line),
            reverse=True,
        )
        for replacement in ordered:
            content = _replace_line_range(
                content,
                replacement.start_line,
                replacement.end_line,
                replacement.replacement,
            )
        if not dry_run:
            file_path.write_text(content, encoding="utf-8")
        logs.append(f"update: {file_path}")

    if not dry_run:
        common_file.parent.mkdir(parents=True, exist_ok=True)
        common_payload = plan.common_code
        if common_payload and not common_payload.endswith("\n"):
            common_payload += "\n"
        common_file.write_text(common_payload, encoding="utf-8")
    logs.append(f"write common: {common_file}")

    return logs
