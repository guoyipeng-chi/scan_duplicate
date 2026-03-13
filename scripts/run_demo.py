from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
DEMO_C = ROOT / "demo_c"
ARTIFACTS = ROOT / "artifacts"


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def _create_demo_branch() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"deduper/demo-{stamp}"
    probe = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
        cwd=DEMO_C,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        branch = f"{branch}-1"

    switch = subprocess.run(
        ["git", "switch", "-c", branch],
        cwd=DEMO_C,
        capture_output=True,
        text=True,
        check=False,
    )
    if switch.returncode != 0:
        _run(["git", "checkout", "-b", branch], cwd=DEMO_C)
    else:
        print(f"$ git switch -c {branch}")
    return branch


def _latest_xml() -> Path:
    report_dirs = sorted(ARTIFACTS.glob("cpd_report_demo_c_*"))
    if not report_dirs:
        raise FileNotFoundError("未找到 CPD 报告目录")
    return report_dirs[-1] / "duplication.xml"


def _ensure_git_identity() -> None:
    name = subprocess.run(
        ["git", "config", "--get", "user.name"],
        cwd=DEMO_C,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    email = subprocess.run(
        ["git", "config", "--get", "user.email"],
        cwd=DEMO_C,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not name:
        _run(["git", "config", "user.name", "demo-bot"], cwd=DEMO_C)
    if not email:
        _run(["git", "config", "user.email", "demo-bot@example.com"], cwd=DEMO_C)


def _reset_demo_sources() -> None:
    template_dir = ROOT / "demo_assets" / "templates" / "src"
    target_dir = DEMO_C / "src"

    target_dir.mkdir(parents=True, exist_ok=True)
    for name in ["metrics_a.c", "metrics_b.c"]:
        shutil.copy2(template_dir / name, target_dir / name)

    common_file = target_dir / "common_math.c"
    if common_file.exists():
        common_file.unlink()


def _recreate_demo_git_repo() -> None:
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=DEMO_C,
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode == 0 and probe.stdout.strip() == "true":
        _run(["git", "reset", "--hard"], cwd=DEMO_C)
        _run(["git", "clean", "-fd"], cwd=DEMO_C)
    else:
        _run(["git", "init"], cwd=DEMO_C)

    _ensure_git_identity()


def _commit_if_changes(message: str) -> None:
    _run(["git", "add", "."], cwd=DEMO_C)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=DEMO_C,
        capture_output=True,
        text=True,
        check=True,
    )
    if status.stdout.strip():
        _run(["git", "commit", "-m", message], cwd=DEMO_C)


def run(mode: str) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    _recreate_demo_git_repo()
    branch = _create_demo_branch()
    print(f"[git] demo 从当前节点创建新分支: {branch}")
    _reset_demo_sources()
    _commit_if_changes("chore: baseline duplicated C demo")

    xml_file: Path
    try:
        _run([
            sys.executable,
            str(ROOT / "scan_c_duplication.py"),
            str(DEMO_C),
            "--out-dir",
            str(ARTIFACTS),
            "--min-tokens",
            "10",
            "--no-ignore-identifiers",
            "--no-ignore-literals",
        ])
        xml_file = _latest_xml()
    except subprocess.CalledProcessError:
        if mode != "offline":
            raise
        xml_file = ROOT / "demo_assets" / "duplication.demo.xml"
        print(f"PMD 不可用，offline 模式改用预置报告: {xml_file}")

    _run([sys.executable, str(ROOT / "main.py"), "list", "--xml", str(xml_file)])

    if mode == "offline":
        _run(
            [
                sys.executable,
                str(ROOT / "main.py"),
                "apply-plan",
                "--plan",
                str(ROOT / "demo_assets" / "refactor_plan_demo.json"),
                "--config",
                str(ROOT / "demo_assets" / "deduper.demo.config.json"),
                "--apply",
            ]
        )
    else:
        _run(
            [
                sys.executable,
                str(ROOT / "main.py"),
                "refactor",
                "--xml",
                str(xml_file),
                "--groups",
                "1",
                "--config",
                str(ROOT / "demo_assets" / "deduper.demo.config.json"),
                "--apply",
            ]
        )

    _commit_if_changes("refactor: extract duplicated normalization logic")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run end-to-end deduper demo")
    parser.add_argument(
        "--mode",
        choices=["offline", "llm"],
        default="offline",
        help="offline uses bundled demo plan, llm calls configured model",
    )
    args = parser.parse_args()

    run(args.mode)
    print("Demo completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
