#!/usr/bin/env python3
"""Fast, token-cheap test runner for any Home Assistant custom-component repo.

Stdlib only; drop this single file into any repo that has `custom_components/<domain>/`
and run it with any Python 3.9+. It needs `uv` (https://docs.astral.sh/uv/) to build
the isolated test venv quickly; it falls back to `python -m venv` + pip without it.

    python scripts/haos_fasttest.py            # static checks + unit/contract tests (default)
    python scripts/haos_fasttest.py --changed  # only tests related to files changed vs HEAD
    python scripts/haos_fasttest.py --lf       # re-run only what failed last time
    python scripts/haos_fasttest.py -k mesh    # pass-through pytest selection
    python scripts/haos_fasttest.py --static   # manifest/compile/ruff only (~1 s)
    python scripts/haos_fasttest.py --all      # also tests needing Docker (integration/)
    python scripts/haos_fasttest.py --full     # don't truncate failure output
    python scripts/haos_fasttest.py setup      # (re)build the venv explicitly

Output is deliberately tiny: one `OK ...` line on success, only the failing tests
(truncated) on failure. Everything else is suppressed so an agent can loop cheaply.

Environment:
    HAOS_TEST_PYTHON   Python version for the venv (default 3.13, HA core's minimum)
    HAOS_TEST_VENV     venv directory (default <repo>/.venv-test)
    HAOS_TEST_MAXLINES failure-output line cap (default 60)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = Path(os.environ.get("HAOS_TEST_VENV", ROOT / ".venv-test"))
PY_VERSION = os.environ.get("HAOS_TEST_PYTHON", "3.13")
MAX_LINES = int(os.environ.get("HAOS_TEST_MAXLINES", "60"))
IS_WIN = sys.platform == "win32"
VENV_PY = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
STAMP = VENV / ".haos-fasttest.stamp"
# Directories (under tests/) that need Docker / external services; skipped unless --all.
HEAVY_DIRS = ("integration", "browser", "e2e")


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, cwd=ROOT, **kw)


def components() -> list[Path]:
    return sorted(p.parent for p in (ROOT / "custom_components").glob("*/manifest.json"))


# --------------------------------------------------------------------------- setup
def wanted_packages() -> list[str]:
    """Packages the tests need: HA test plugin + ruff + every manifest requirement."""
    pkgs = ["pytest-homeassistant-custom-component", "ruff"]
    for comp in components():
        pkgs += json.loads((comp / "manifest.json").read_text("utf-8")).get("requirements", [])
    tests = ROOT / "tests"
    if tests.is_dir() and any("testcontainers" in t.read_text("utf-8", errors="ignore")
                              for t in tests.rglob("*.py")):
        pkgs.append("testcontainers")
    for extra in ("requirements_test.txt", "requirements-test.txt", "requirements_dev.txt"):
        f = ROOT / extra
        if f.exists():
            pkgs += [ln.strip() for ln in f.read_text("utf-8").splitlines()
                     if ln.strip() and not ln.startswith(("#", "-"))]
    return sorted(set(pkgs))


def write_overrides() -> Path:
    """HA pins lru-dict==1.3.0, which has no cp313 wheel (needs a C compiler on Windows)."""
    VENV.mkdir(parents=True, exist_ok=True)
    f = VENV / "overrides.txt"
    f.write_text("lru-dict>=1.4\n")
    return f


def ensure_venv(force: bool = False) -> None:
    pkgs = wanted_packages()
    sig = hashlib.sha256(f"{PY_VERSION}|{pkgs}".encode()).hexdigest()
    if not force and VENV_PY.exists() and STAMP.exists() and STAMP.read_text() == sig:
        return
    print(f"[setup] building {VENV.name} (python {PY_VERSION}, {len(pkgs)} packages)...", flush=True)
    uv = shutil.which("uv")
    if uv:
        # `uv python find` returns a concrete interpreter path; more robust than the bare
        # version (uv's minor-version link can be broken on some Windows installs).
        found = sh([uv, "python", "find", PY_VERSION])
        if found.returncode:
            sh([uv, "python", "install", PY_VERSION])
            found = sh([uv, "python", "find", PY_VERSION])
        interp = found.stdout.strip() or PY_VERSION
        steps = [
            [uv, "venv", "--python", interp, "--allow-existing", str(VENV)],
            [uv, "pip", "install", "--python", str(VENV_PY), "--override", str(write_overrides()),
             *pkgs],
        ]
    else:
        steps = [
            [sys.executable, "-m", "venv", str(VENV)],
            [str(VENV_PY), "-m", "pip", "install", "-q", *pkgs],
        ]
    for step in steps:
        r = sh(step)
        if r.returncode:
            sys.exit(f"[setup] FAILED: {' '.join(step[:4])}...\n{(r.stderr or r.stdout)[-1500:]}")
    STAMP.write_text(sig)
    print("[setup] done", flush=True)


# ------------------------------------------------------------------- static checks
def static_checks() -> list[str]:
    """Cheap checks that catch most mistakes in ~1s. Returns list of problems."""
    problems: list[str] = []
    for comp in components():
        try:
            m = json.loads((comp / "manifest.json").read_text("utf-8"))
        except ValueError as e:
            problems.append(f"{comp.name}/manifest.json invalid JSON: {e}")
            continue
        for key in ("domain", "name", "version"):
            if key not in m:
                problems.append(f"{comp.name}/manifest.json missing '{key}'")
        if m.get("domain") != comp.name:
            problems.append(f"{comp.name}/manifest.json domain={m.get('domain')!r} != dir name")
        for svc in comp.glob("*.yaml"):
            if not svc.read_text("utf-8").strip():
                problems.append(f"{comp.name}/{svc.name} is empty")
    r = sh([str(VENV_PY), "-m", "compileall", "-q", "custom_components"])
    if r.returncode:
        problems.append("compile errors:\n" + (r.stdout + r.stderr).strip()[-800:])
    r = sh([str(VENV_PY), "-m", "ruff", "check", "--no-fix", "--select", "F,E9", "--output-format=concise",
            "custom_components"])
    if r.returncode:
        problems.append("ruff:\n" + r.stdout.strip()[-1500:])
    return problems


# --------------------------------------------------------------- test selection
def changed_files() -> list[str]:
    out = sh(["git", "diff", "--name-only", "HEAD"]).stdout
    out += sh(["git", "ls-files", "--others", "--exclude-standard"]).stdout
    return [ln for ln in out.splitlines() if ln.endswith(".py")]


def related_tests(changed: list[str], include_heavy: bool) -> list[str]:
    """Map changed source files to tests that mention their module name."""
    tests_dir = ROOT / "tests"
    all_tests = [t for t in tests_dir.rglob("test_*.py")
                 if include_heavy or t.relative_to(tests_dir).parts[0] not in HEAVY_DIRS]
    picked: set[str] = set()
    for f in changed:
        p = Path(f)
        if p.parts[:1] == ("tests",) and p.name.startswith("test_") and (ROOT / f).exists():
            if include_heavy or p.parts[1] not in HEAVY_DIRS:
                picked.add(f)
        elif p.parts[:1] == ("custom_components",) and p.stem != "__init__":
            pat = re.compile(rf"\b{re.escape(p.stem)}\b")
            for t in all_tests:
                if pat.search(t.read_text("utf-8", errors="ignore")):
                    picked.add(t.relative_to(ROOT).as_posix())
        elif p.name == "conftest.py" or p.name == "__init__.py":
            return []  # shared fixtures changed -> run everything
    return sorted(picked)


# ------------------------------------------------------------------------- pytest
def win_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # pytest-socket only allows 127.0.0.1; testcontainers (Ryuk) would use "localhost".
    env.setdefault("TC_HOST", "127.0.0.1")
    if IS_WIN:
        # HA core imports POSIX-only fcntl/resource; provide no-op stand-ins if the repo has none.
        stubs = ROOT / "tests" / "_winstubs"
        if not stubs.exists():
            stubs = VENV / "_winstubs"
            stubs.mkdir(exist_ok=True)
            (stubs / "fcntl.py").write_text(
                "LOCK_EX=LOCK_SH=LOCK_UN=LOCK_NB=0\n"
                "def flock(*a, **k): pass\ndef lockf(*a, **k): pass\n"
                "def fcntl(*a, **k): return 0\ndef ioctl(*a, **k): return 0\n")
            (stubs / "resource.py").write_text(
                "RLIMIT_NOFILE=7\nRLIM_INFINITY=-1\n"
                "def getrlimit(*a): return (1024, 1024)\ndef setrlimit(*a): pass\n")
        env["PYTHONPATH"] = str(stubs) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def trim(output: str, full: bool) -> str:
    """Keep the failure sections + summary; drop progress dots and noise."""
    lines = [ln for ln in output.splitlines() if not re.fullmatch(r"[.sxFE\s%\[\]\d]*", ln)]
    text = "\n".join(lines)
    m = re.search(r"^=+ (FAILURES|ERRORS) =+$", text, re.M)
    if m:
        text = text[m.start():]
    lines = text.splitlines()
    if full or len(lines) <= MAX_LINES:
        return "\n".join(lines)
    head, tail = lines[: MAX_LINES - 15], lines[-12:]
    return "\n".join(head + [f"... [{len(lines) - len(head) - len(tail)} lines cut; use --full]"] + tail)


def run_pytest(targets: list[str], args: argparse.Namespace) -> int:
    cmd = [str(VENV_PY), "-m", "pytest", "-q", "--no-header", "--tb=short", "-rfE",
           "-W", "ignore", "-p", "no:randomly"]
    if args.lf:
        cmd.append("--lf")
    if not args.keep_going:
        cmd.append("-x")
    if args.all:
        # HA's test plugin blocks sockets per test, which breaks the
        # Docker/Memgraph connections of integration tests. Same shim as tests/conftest.py (win32).
        plugin = [
            "import pytest_socket",
            "def _noop(*a, **k):",
            "    pass",
            "pytest_socket.disable_socket = _noop",
            "pytest_socket.socket_allow_hosts = _noop",
        ]
        (VENV / "haos_ft_unix_socket.py").write_text(chr(10).join(plugin) + chr(10))
        cmd += ["-p", "haos_ft_unix_socket"]
    else:
        for d in HEAVY_DIRS:
            if (ROOT / "tests" / d).is_dir():
                cmd += [f"--ignore=tests/{d}"]
    cmd += args.pytest_args + targets
    t0 = time.time()
    env = win_env()
    env["PYTHONPATH"] = str(VENV) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(cmd, cwd=ROOT, env=env, text=True, capture_output=True,
                       encoding="utf-8", errors="replace")
    dt = time.time() - t0
    out = r.stdout + r.stderr
    summary = next((ln.strip("= ") for ln in reversed(out.splitlines())
                    if re.search(r"\d+ (passed|failed|error|skipped|deselected)", ln)), "no tests ran")
    if r.returncode == 0:
        print(f"OK tests: {summary} [{dt:.0f}s]")
    else:
        print(trim(out, args.full))
        print(f"FAIL tests: {summary} [{dt:.0f}s]")
    return r.returncode


def docker_ok() -> bool:
    return shutil.which("docker") is not None and sh(["docker", "info"]).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--changed", action="store_true", help="only tests related to changed files")
    ap.add_argument("--lf", action="store_true", help="re-run last failures only")
    ap.add_argument("--static", action="store_true", help="static checks only")
    ap.add_argument("--all", action="store_true", help="include Docker-dependent tests")
    ap.add_argument("--full", action="store_true", help="no output truncation")
    ap.add_argument("--keep-going", action="store_true", help="don't stop at first failure")
    ap.add_argument("--skip-static", action="store_true")
    ap.add_argument("pytest_args", nargs="*", help="extra args for pytest (prefix with --)")
    args = ap.parse_args()
    args.command = "run"
    if args.pytest_args[:1] == ["setup"]:
        args.command, args.pytest_args = "setup", args.pytest_args[1:]

    if not (ROOT / "custom_components").is_dir():
        sys.exit("no custom_components/ directory found")
    ensure_venv(force=args.command == "setup")
    if args.command == "setup":
        return 0

    if not args.skip_static:
        problems = static_checks()
        if problems:
            print("FAIL static:\n" + "\n".join(problems))
            return 1
        print("OK static")
        if args.static:
            return 0

    if args.all and not docker_ok():
        print("WARN --all requested but Docker is not running; integration tests will error")

    targets: list[str] = []
    if args.changed:
        targets = related_tests(changed_files(), args.all)
        if targets:
            print(f"[changed] {len(targets)} related test file(s)")
        else:
            print("[changed] no mapping found, running full fast suite")
    return run_pytest(targets, args)


if __name__ == "__main__":
    sys.exit(main())
