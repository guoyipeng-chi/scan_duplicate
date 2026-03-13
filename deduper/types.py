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
                replacement=str(item["replacement"]),
            )
            for item in payload.get("replacements", [])
        ]
        return LLMRefactorPlan(
            common_file=Path(payload["common_file"]),
            common_code=str(payload.get("common_code", "")),
            replacements=replacements,
            notes=str(payload.get("notes", "")),
        )
