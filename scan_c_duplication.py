from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile


PMD_VERSION = "7.22.0"
PMD_DOWNLOAD_URL = f"https://github.com/pmd/pmd/releases/download/pmd_releases%2F{PMD_VERSION}/pmd-dist-{PMD_VERSION}-bin.zip"


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _run_capture(command: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=check)


def _which(candidates: list[str]) -> Path | None:
    for item in candidates:
        result = shutil.which(item)
        if result:
            return Path(result)
    return None


def _set_java_home_from_known_locations() -> None:
    if os.getenv("JAVA_HOME"):
        return

    if os.name == "nt":
        roots = [
            Path("C:/Program Files/Eclipse Adoptium"),
            Path("C:/Program Files/Java"),
        ]
        for root in roots:
            if not root.exists():
                continue
            java_candidates = sorted(root.glob("**/bin/java.exe"), reverse=True)
            if java_candidates:
                java_home = str(java_candidates[0].parent.parent)
                os.environ["JAVA_HOME"] = java_home
                os.environ["PATH"] = str(java_candidates[0].parent) + os.pathsep + os.environ.get("PATH", "")
                return
        return

    roots = [Path("/usr/lib/jvm"), Path("/usr/java")]
    for root in roots:
        if not root.exists():
            continue
        java_candidates = sorted(root.glob("**/bin/java"), reverse=True)
        if java_candidates:
            java_home = str(java_candidates[0].parent.parent)
            os.environ["JAVA_HOME"] = java_home
            os.environ["PATH"] = str(java_candidates[0].parent) + os.pathsep + os.environ.get("PATH", "")
            return


def _detect_java() -> Path | None:
    java_home = os.getenv("JAVA_HOME")
    if java_home:
        suffix = "java.exe" if os.name == "nt" else "java"
        java_from_home = Path(java_home) / "bin" / suffix
        if java_from_home.exists():
            return java_from_home

    return _which(["java"])


def _java_ok() -> bool:
    java = _detect_java()
    if not java:
        return False
    result = _run_capture([str(java), "-version"], check=False)
    return result.returncode == 0


def _install_java_windows() -> None:
    winget = shutil.which("winget")
    if not winget:
        raise RuntimeError("Windows 未找到 winget，无法自动安装 Java。")

    _run(
        [
            winget,
            "install",
            "-e",
            "--id",
            "EclipseAdoptium.Temurin.17.JDK",
            "--accept-package-agreements",
            "--accept-source-agreements",
            "--silent",
        ]
    )


def _install_java_linux() -> None:
    package_managers: list[list[str]] = []

    if shutil.which("apt-get"):
        package_managers.append(["apt-get", "update"])
        package_managers.append(["apt-get", "install", "-y", "openjdk-17-jre-headless"])
    elif shutil.which("dnf"):
        package_managers.append(["dnf", "install", "-y", "java-17-openjdk"])
    elif shutil.which("yum"):
        package_managers.append(["yum", "install", "-y", "java-17-openjdk"])
    elif shutil.which("pacman"):
        package_managers.append(["pacman", "-Sy", "--noconfirm", "jre17-openjdk"])
    elif shutil.which("zypper"):
        package_managers.append(["zypper", "--non-interactive", "install", "java-17-openjdk"])
    else:
        raise RuntimeError("Linux 未找到支持的包管理器，无法自动安装 Java。")

    use_sudo = shutil.which("sudo") is not None and os.geteuid() != 0
    for command in package_managers:
        if use_sudo:
            _run(["sudo"] + command)
        else:
            _run(command)


def ensure_java(auto_install: bool) -> Path:
    _set_java_home_from_known_locations()
    java = _detect_java()
    if java and _java_ok():
        return java

    if not auto_install:
        raise RuntimeError("未检测到可用 Java。请安装 Java 17+，或使用 --auto-install-java 自动安装。")

    print("Java 未就绪，尝试自动安装...")
    if os.name == "nt":
        _install_java_windows()
    elif os.name == "posix":
        _install_java_linux()
    else:
        raise RuntimeError("当前平台暂不支持自动安装 Java，请手动安装 Java 17+。")

    _set_java_home_from_known_locations()
    java = _detect_java()
    if not java or not _java_ok():
        raise RuntimeError("Java 自动安装后仍不可用，请重新打开终端并执行 java -version 检查。")

    print(f"Java ready: {java}")
    return java


def _detect_pmd_cli(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))

    env_bin = os.getenv("PMD_BIN")
    if env_bin:
        candidates.append(Path(env_bin))

    local_bin = _local_pmd_bin_dir()
    candidates.extend([local_bin / "pmd.bat", local_bin / "pmd"])

    if os.name == "nt":
        candidates.extend(
            [
                Path.home() / "tool" / "pmd-bin-7.22.0" / "bin" / "pmd.bat",
                Path.home() / "tool" / "pmd-bin-7.22.0" / "bin" / "pmd",
            ]
        )
    else:
        candidates.extend(
            [
                Path.home() / "tool" / "pmd-bin-7.22.0" / "bin" / "pmd",
                Path.home() / "tool" / "pmd-bin-7.22.0" / "bin" / "pmd.bat",
            ]
        )

    which_pmd = shutil.which("pmd")
    if which_pmd:
        candidates.append(Path(which_pmd))
    which_pmd_bat = shutil.which("pmd.bat")
    if which_pmd_bat:
        candidates.append(Path(which_pmd_bat))

    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        "找不到 PMD 可执行文件。可通过 --pmd 指定，或设置 PMD_BIN 环境变量。"
    )


def _local_pmd_bin_dir() -> Path:
    return Path.cwd() / ".tools" / f"pmd-bin-{PMD_VERSION}" / "bin"


def _install_pmd_local() -> Path:
    bin_dir = _local_pmd_bin_dir()
    if os.name == "nt":
        ready = bin_dir / "pmd.bat"
    else:
        ready = bin_dir / "pmd"
    if ready.exists():
        return ready

    bin_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "pmd.zip"
        print(f"下载 PMD: {PMD_DOWNLOAD_URL}")
        urllib.request.urlretrieve(PMD_DOWNLOAD_URL, zip_path)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(Path(tmp))

        extracted_root = Path(tmp) / f"pmd-bin-{PMD_VERSION}"
        if not extracted_root.exists():
            raise RuntimeError("PMD 压缩包结构异常，未找到解压目录。")

        target_root = Path.cwd() / ".tools" / f"pmd-bin-{PMD_VERSION}"
        if target_root.exists():
            shutil.rmtree(target_root)
        shutil.copytree(extracted_root, target_root)

    if not ready.exists():
        raise RuntimeError("PMD 安装失败，未找到可执行文件。")
    return ready


def ensure_pmd(explicit: str | None, auto_install_pmd: bool) -> Path:
    try:
        return _detect_pmd_cli(explicit)
    except FileNotFoundError:
        if not auto_install_pmd:
            raise
        print("未检测到 PMD，尝试自动安装到 .tools 目录...")
        pmd_cli = _install_pmd_local()
        os.environ["PMD_BIN"] = str(pmd_cli)
        local_bin = pmd_cli.parent
        os.environ["PATH"] = str(local_bin) + os.pathsep + os.environ.get("PATH", "")
        return pmd_cli


def _validate_pmd_runtime(pmd_cli: Path, auto_install_java: bool) -> None:
    first = _run_capture([str(pmd_cli), "--version"], check=False)
    if first.returncode == 0:
        return

    merged = (first.stdout or "") + "\n" + (first.stderr or "")
    if "No java executable found in PATH" in merged or "java" in merged.lower():
        ensure_java(auto_install=auto_install_java)
        second = _run_capture([str(pmd_cli), "--version"], check=False)
        if second.returncode == 0:
            return

    raise RuntimeError(
        "PMD 无法启动。请检查 PMD 安装和 Java 环境。"
        f"\nstdout:\n{first.stdout}\nstderr:\n{first.stderr}"
    )


def _run_pmd_and_expect_report(command: list[str], report_file: Path) -> None:
    result = _run_capture(command, check=False)
    if result.returncode in {0, 1, 4} and report_file.exists():
        return

    def _normalize_output(label: str, content: str | None) -> str:
        text = (content or "").strip()
        if not text:
            return f"{label}: <empty>"
        preview = text if len(text) <= 8000 else text[:8000] + "\n...[truncated]"
        return f"{label}:\n{preview}"

    report_exists = report_file.exists()
    report_size = report_file.stat().st_size if report_exists else 0

    env_checks: list[str] = []
    try:
        pmd_version = _run_capture([command[0], "--version"], check=False)
        env_checks.append(f"PMD --version exit={pmd_version.returncode}")
        env_checks.append(_normalize_output("PMD --version stdout", pmd_version.stdout))
        env_checks.append(_normalize_output("PMD --version stderr", pmd_version.stderr))
    except Exception as exc:
        env_checks.append(f"PMD --version check failed: {exc}")

    try:
        java = _detect_java()
        if java:
            java_version = _run_capture([str(java), "-version"], check=False)
            env_checks.append(f"java path={java}")
            env_checks.append(f"java -version exit={java_version.returncode}")
            env_checks.append(_normalize_output("java -version stdout", java_version.stdout))
            env_checks.append(_normalize_output("java -version stderr", java_version.stderr))
        else:
            env_checks.append("java path=<not found>")
    except Exception as exc:
        env_checks.append(f"java check failed: {exc}")

    hint = ""
    if result.returncode == 5:
        hint = (
            "\n提示: PMD 退出码 5 常见于运行时异常（参数组合、语言设置、Java/PMD 兼容性或扫描目录权限问题）。"
            "请重点检查 --language、repo 路径可读性，以及 PMD/Java 版本输出。"
        )

    raise RuntimeError(
        f"PMD 执行失败，退出码={result.returncode}\n"
        f"command: {' '.join(command)}\n"
        f"cwd: {Path.cwd()}\n"
        f"report_file: {report_file}\n"
        f"report_exists: {report_exists}, report_size={report_size}\n"
        f"{_normalize_output('stdout', result.stdout)}\n"
        f"{_normalize_output('stderr', result.stderr)}\n"
        f"runtime_checks:\n" + "\n".join(env_checks)
        + hint
    )


def _count_c_files(repo: Path) -> int:
    return sum(
        1
        for path in repo.rglob("*")
        if path.is_file() and path.suffix.lower() in {".c", ".h", ".cpp", ".hpp"}
    )


def _count_duplications(xml_file: Path) -> int:
    if not xml_file.exists():
        return 0
    tree = ET.parse(xml_file)
    root = tree.getroot()
    count = 0
    for node in root.iter():
        tag = node.tag
        name = tag.rsplit("}", 1)[-1] if "}" in tag else tag
        if name == "duplication":
            count += 1
    return count


def scan(
    repo: Path,
    out_base: Path,
    pmd_cli: Path,
    min_tokens: int,
    language: str,
    ignore_identifiers: bool,
    ignore_literals: bool,
) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = out_base / f"cpd_report_{repo.name}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    xml_report = out_dir / "duplication.xml"
    md_report = out_dir / "duplication.md"

    print(f"Repository : {repo}")
    print(f"Output dir : {out_dir}")
    print("Running CPD scan...")

    base_args = [
        str(pmd_cli),
        "cpd",
        "--dir",
        str(repo),
        "--language",
        language,
        "--minimum-tokens",
        str(min_tokens),
    ]
    if ignore_identifiers:
        base_args.append("--ignore-identifiers")
    if ignore_literals:
        base_args.append("--ignore-literals")

    _run_pmd_and_expect_report(
        base_args + ["--format", "xml", "--report-file", str(xml_report)],
        xml_report,
    )
    _run_pmd_and_expect_report(
        base_args + ["--format", "markdown", "--report-file", str(md_report)],
        md_report,
    )

    total_files = _count_c_files(repo)
    total_dup = _count_duplications(xml_report)

    summary = out_dir / "summary.txt"
    summary.write_text(
        "\n".join(
            [
                "CPD DUPLICATION REPORT",
                "======================",
                "",
                f"Repository: {repo}",
                f"Scan time: {dt.datetime.now().isoformat()}",
                f"Total C-family files: {total_files}",
                f"Duplicate blocks: {total_dup}",
                "",
                f"Markdown report: {md_report}",
                f"Raw XML: {xml_report}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print("Scan finished")
    print(summary.read_text(encoding="utf-8"))
    return out_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cross-platform CPD scanner for C/C++")
    parser.add_argument("repo", help="Repository path to scan")
    parser.add_argument(
        "--out-dir",
        default=str(Path.cwd() / "artifacts"),
        help="Base output directory",
    )
    parser.add_argument("--pmd", required=False, help="PMD executable path")
    parser.add_argument(
        "--auto-install-pmd",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="PMD 不可用时是否自动下载并安装到 .tools（默认开启）",
    )
    parser.add_argument("--min-tokens", type=int, default=40, help="CPD min tokens")
    parser.add_argument(
        "--auto-install-java",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Java 不可用时是否自动安装（默认开启）",
    )
    parser.add_argument(
        "--language",
        default="cpp",
        choices=["cpp", "c"],
        help="CPD language",
    )
    parser.add_argument(
        "--ignore-identifiers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否忽略标识符（默认开启）",
    )
    parser.add_argument(
        "--ignore-literals",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否忽略字面量（默认开启）",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo = Path(args.repo).resolve()
    if not repo.exists() or not repo.is_dir():
        print(f"ERROR: repo path not found: {repo}", file=sys.stderr)
        return 1

    ensure_java(auto_install=args.auto_install_java)
    pmd_cli = ensure_pmd(args.pmd, auto_install_pmd=args.auto_install_pmd)
    _validate_pmd_runtime(pmd_cli, auto_install_java=args.auto_install_java)
    out_base = Path(args.out_dir).resolve()
    out_base.mkdir(parents=True, exist_ok=True)

    scan(
        repo,
        out_base,
        pmd_cli,
        args.min_tokens,
        args.language,
        ignore_identifiers=args.ignore_identifiers,
        ignore_literals=args.ignore_literals,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
