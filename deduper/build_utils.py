from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Iterable

from .config import BuildStepConfig


class BuildFailedError(RuntimeError):
    pass


def _resolve_cwd(workspace: Path, cwd: str) -> Path:
    path = Path(cwd)
    if path.is_absolute():
        return path
    return (workspace / path).resolve()


def _expand_args(command: Iterable[str], working_dir: Path) -> list[str]:
    expanded: list[str] = []
    for item in command:
        if any(char in item for char in ["*", "?", "["]):
            matches = sorted(working_dir.glob(item))
            if matches:
                expanded.extend(str(match) for match in matches)
                continue
        expanded.append(item)
    return expanded


def run_build_step(workspace: Path, step: BuildStepConfig | None, stage: str) -> None:
    if step is None:
        return

    working_dir = _resolve_cwd(workspace, step.cwd)
    if not working_dir.exists():
        raise BuildFailedError(f"[{stage}] 构建目录不存在: {working_dir}")

    if isinstance(step.command, str):
        command = step.command
        shell = True if not step.shell else step.shell
    else:
        command = _expand_args([str(item) for item in step.command], working_dir)
        shell = step.shell

    result = subprocess.run(
        command,
        cwd=working_dir,
        capture_output=True,
        text=True,
        shell=shell,
        check=False,
    )
    if result.returncode != 0:
        raise BuildFailedError(
            f"[{stage}] 编译失败（exit={result.returncode}）\n"
            f"cwd: {working_dir}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
