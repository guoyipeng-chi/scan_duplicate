from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .types import LLMLineOpsPlan, LineCutPasteOp, LineDeleteOp, LineInsertOp


def _in_any_range(line: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= line <= end for start, end in ranges)


def _validate_line_ops_plan(workspace: Path, plan: LLMLineOpsPlan) -> None:
    def resolve(p: Path) -> Path:
        target = (workspace / p).resolve() if not p.is_absolute() else p.resolve()
        if not str(target).startswith(str(workspace.resolve())):
            raise ValueError(f"line-op file 越界: {p}")
        return target

    delete_ranges_by_file: dict[Path, list[tuple[int, int]]] = defaultdict(list)

    for operation in plan.delete:
        file_path = resolve(operation.file)
        lines = _read_lines(file_path)
        if operation.start_line < 1 or operation.end_line < operation.start_line or operation.end_line > len(lines):
            raise ValueError(
                f"delete 非法行区间: {operation.file} start={operation.start_line}, end={operation.end_line}, file_total={len(lines)}"
            )
        delete_ranges_by_file[file_path].append((operation.start_line, operation.end_line))

    for operation in plan.cut_paste:
        source = resolve(operation.source_file)
        source_lines = _read_lines(source)
        if operation.start_line < 1 or operation.end_line < operation.start_line or operation.end_line > len(source_lines):
            raise ValueError(
                f"cut 非法行区间: {operation.source_file} start={operation.start_line}, end={operation.end_line}, file_total={len(source_lines)}"
            )
        target = resolve(operation.target_file)
        target_lines = _read_lines(target)
        if operation.target_line < 1 or operation.target_line > len(target_lines):
            raise ValueError(
                f"cut_paste 非法目标行: {operation.target_file} line={operation.target_line}, file_total={len(target_lines)}"
            )
        delete_ranges_by_file[source].append((operation.start_line, operation.end_line))
        if not target.exists() and operation.target_line != 1:
            raise ValueError(
                f"cut_paste 目标文件不存在时 target_line 必须为 1: {operation.target_file} line={operation.target_line}"
            )

    for operation in plan.insert:
        file_path = resolve(operation.file)
        if file_path.exists():
            lines = _read_lines(file_path)
            if operation.line < 1 or operation.line > len(lines):
                raise ValueError(
                    f"insert 非法行号: {operation.file} line={operation.line}, file_total={len(lines)}"
                )
        else:
            if operation.line != 1:
                raise ValueError(
                    f"insert 到新文件时 line 必须为 1: {operation.file} line={operation.line}"
                )

    for operation in plan.insert:
        file_path = resolve(operation.file)
        ranges = delete_ranges_by_file.get(file_path, [])
        if _in_any_range(operation.line, ranges):
            raise ValueError(
                f"insert 冲突：目标行位于被删除区间内: {operation.file} line={operation.line}, delete_ranges={ranges}"
            )

    for operation in plan.cut_paste:
        target = resolve(operation.target_file)
        ranges = delete_ranges_by_file.get(target, [])
        if _in_any_range(operation.target_line, ranges):
            raise ValueError(
                f"cut_paste 冲突：目标行位于被删除区间内: {operation.target_file} line={operation.target_line}, delete_ranges={ranges}"
            )


def _replace_range(lines: list[str], start_line: int, end_line: int, replacement: list[str]) -> list[str]:
    total = len(lines)
    if start_line < 1 or end_line < start_line or end_line > total:
        raise ValueError(f"非法行区间: start={start_line}, end={end_line}, file_total={total}")
    return lines[: start_line - 1] + replacement + lines[end_line:]


def _insert_lines(lines: list[str], line: int, position: str, content: str) -> list[str]:
    total = len(lines)
    if line < 1 or line > max(1, total):
        raise ValueError(f"非法插入位置: line={line}, file_total={total}")

    payload = content
    if payload and not payload.endswith("\n"):
        payload += "\n"
    block = payload.splitlines(keepends=True)

    if position == "before":
        index = line - 1
    else:
        index = line
    return lines[:index] + block + lines[index:]


def _read_lines(path: Path, allow_missing: bool = False) -> list[str]:
    if not path.exists():
        if allow_missing:
            return []
        raise FileNotFoundError(f"待修改文件不存在: {path}")
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _write_lines(path: Path, lines: list[str], dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


def apply_line_ops_plan(workspace: Path, plan: LLMLineOpsPlan, dry_run: bool = True) -> list[str]:
    logs: list[str] = []
    _validate_line_ops_plan(workspace, plan)

    def resolve(p: Path) -> Path:
        target = (workspace / p).resolve() if not p.is_absolute() else p.resolve()
        if not str(target).startswith(str(workspace.resolve())):
            raise ValueError(f"line-op file 越界: {p}")
        return target

    file_cache: dict[Path, list[str]] = {}
    missing_allowed_files: set[Path] = set()
    for operation in plan.insert:
        target = resolve(operation.file)
        if not target.exists():
            missing_allowed_files.add(target)
    for operation in plan.cut_paste:
        target = resolve(operation.target_file)
        if not target.exists():
            missing_allowed_files.add(target)

    def get_lines(path: Path) -> list[str]:
        if path not in file_cache:
            file_cache[path] = _read_lines(path, allow_missing=path in missing_allowed_files)
        return file_cache[path]

    # 1) collect cut blocks from original files
    cut_blocks: list[tuple[LineCutPasteOp, list[str]]] = []
    for operation in plan.cut_paste:
        source = resolve(operation.source_file)
        source_lines = get_lines(source)
        if operation.start_line < 1 or operation.end_line < operation.start_line or operation.end_line > len(source_lines):
            raise ValueError(
                f"cut 非法行区间: {operation.source_file} start={operation.start_line}, end={operation.end_line}, file_total={len(source_lines)}"
            )
        block = source_lines[operation.start_line - 1 : operation.end_line]
        cut_blocks.append((operation, block))

    # 2) apply deletions (including cut deletions) by file descending ranges
    deletions_by_file: dict[Path, list[tuple[int, int]]] = defaultdict(list)
    for operation in plan.delete:
        target = resolve(operation.file)
        deletions_by_file[target].append((operation.start_line, operation.end_line))
    for operation, _ in cut_blocks:
        source = resolve(operation.source_file)
        deletions_by_file[source].append((operation.start_line, operation.end_line))

    for file_path, ranges in deletions_by_file.items():
        lines = get_lines(file_path)
        for start_line, end_line in sorted(ranges, key=lambda item: (item[0], item[1]), reverse=True):
            lines = _replace_range(lines, start_line, end_line, [])
        file_cache[file_path] = lines

    # 3) apply inserts from cut blocks
    inserts_by_file: dict[Path, list[LineInsertOp]] = defaultdict(list)
    for operation, block in cut_blocks:
        target = resolve(operation.target_file)
        payload = "".join(block)
        inserts_by_file[target].append(
            LineInsertOp(
                file=operation.target_file,
                line=operation.target_line,
                position=operation.position,
                content=payload,
            )
        )

    for operation in plan.insert:
        target = resolve(operation.file)
        inserts_by_file[target].append(operation)

    for file_path, operations in inserts_by_file.items():
        lines = get_lines(file_path)
        before_ops = sorted((op for op in operations if op.position == "before"), key=lambda item: item.line, reverse=True)
        after_ops = sorted((op for op in operations if op.position != "before"), key=lambda item: item.line, reverse=True)
        for operation in before_ops + after_ops:
            lines = _insert_lines(lines, operation.line, operation.position, operation.content)
        file_cache[file_path] = lines

    for file_path, lines in file_cache.items():
        _write_lines(file_path, lines, dry_run=dry_run)
        logs.append(f"update: {file_path}")

    return logs
