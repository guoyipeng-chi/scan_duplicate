from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import subprocess


class GitRepoError(RuntimeError):
    pass


def _run_git(repo: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


def is_git_repo(repo: Path) -> bool:
    probe = _run_git(repo, ["rev-parse", "--is-inside-work-tree"], check=False)
    return probe.returncode == 0 and probe.stdout.strip() == "true"


def sanitize_branch_name(raw: str) -> str:
    cleaned = raw.strip().replace(" ", "-")
    cleaned = re.sub(r"[^A-Za-z0-9._/-]", "-", cleaned)
    cleaned = re.sub(r"-+", "-", cleaned)
    cleaned = cleaned.strip("-./")
    return cleaned or "deduper/refactor"


def generate_branch_name(prefix: str = "deduper/refactor") -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return sanitize_branch_name(f"{prefix}-{stamp}")


def create_branch_from_current(repo: Path, preferred_name: str | None = None) -> str:
    if not is_git_repo(repo):
        raise GitRepoError(f"不是 Git 仓库: {repo}")

    branch_name = sanitize_branch_name(preferred_name or generate_branch_name())
    candidate = branch_name
    index = 1
    while True:
        exists = _run_git(repo, ["show-ref", "--verify", f"refs/heads/{candidate}"], check=False)
        if exists.returncode != 0:
            break
        index += 1
        candidate = f"{branch_name}-{index}"

    created = _run_git(repo, ["switch", "-c", candidate], check=False)
    if created.returncode != 0:
        checkout = _run_git(repo, ["checkout", "-b", candidate], check=False)
        if checkout.returncode != 0:
            raise GitRepoError(
                "创建分支失败:\n"
                f"switch: {created.stderr.strip()}\n"
                f"checkout: {checkout.stderr.strip()}"
            )

    return candidate
