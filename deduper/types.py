from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class DuplicationOccurrence:
    path: Path
    line: int
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None


@dataclass
class DuplicationGroup:
    id: int
    lines: int
    tokens: int
    occurrences: list[DuplicationOccurrence] = field(default_factory=list)
    code_fragment: str = ""


@dataclass
class LLMReplacement:
    file: Path
    start_line: int
    end_line: int
    replacement: str


@dataclass
class LLMRefactorPlan:
    common_file: Path
    common_code: str
    replacements: list[LLMReplacement]
    notes: str = ""

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "LLMRefactorPlan":
        replacements = [
            LLMReplacement(
                file=Path(item["file"]),
                start_line=int(item["start_line"]),
                end_line=int(item["end_line"]),
                replacement=str(item.get("replacement", "")),
            )
            for item in payload.get("replacements", [])
        ]
        return LLMRefactorPlan(
            common_file=Path(payload.get("common_file", "include/deduper_common.h")),
            common_code=str(payload.get("common_code", "")),
            replacements=replacements,
            notes=str(payload.get("notes", "")),
        )


@dataclass
class LineCutPasteOp:
    source_file: Path
    start_line: int
    end_line: int
    target_file: Path
    target_line: int
    position: str = "after"


@dataclass
class LineDeleteOp:
    file: Path
    start_line: int
    end_line: int


@dataclass
class LineInsertOp:
    file: Path
    line: int
    position: str
    content: str


@dataclass
class LLMLineOpsPlan:
    cut_paste: list[LineCutPasteOp] = field(default_factory=list)
    delete: list[LineDeleteOp] = field(default_factory=list)
    insert: list[LineInsertOp] = field(default_factory=list)
    notes: str = ""

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "LLMLineOpsPlan":
        ops = payload.get("operations", payload)
        cut_paste = [
            LineCutPasteOp(
                source_file=Path(item["source_file"]),
                start_line=int(item["start_line"]),
                end_line=int(item["end_line"]),
                target_file=Path(item["target_file"]),
                target_line=int(item["target_line"]),
                position=str(item.get("position", "after")),
            )
            for item in ops.get("cut_paste", [])
        ]
        delete = [
            LineDeleteOp(
                file=Path(item["file"]),
                start_line=int(item["start_line"]),
                end_line=int(item["end_line"]),
            )
            for item in ops.get("delete", [])
        ]
        insert = [
            LineInsertOp(
                file=Path(item["file"]),
                line=int(item["line"]),
                position=str(item.get("position", "after")),
                content=str(item.get("content", "")),
            )
            for item in ops.get("insert", [])
        ]
        return LLMLineOpsPlan(
            cut_paste=cut_paste,
            delete=delete,
            insert=insert,
            notes=str(payload.get("notes", ops.get("notes", ""))),
        )
