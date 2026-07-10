"""Preflight check: verifies the Claude Code CLI is installed, authenticated, and speaks the flags we need.

Run standalone:  python check_cli.py
Also imported by main.py at startup (run_preflight returns (ok, message)).
"""

from __future__ import annotations

import shutil
import subprocess
import sys

ECHO_TIMEOUT = 60


def _run(args: list[str], *, stdin_text: str | None = None, timeout: int) -> subprocess.CompletedProcess:
    # encoding= matters: text=True alone decodes the CLI's output with the locale codepage
    # (cp1252 on Windows) in strict mode, which can raise UnicodeDecodeError on non-ASCII
    # output -- see agents/build.py's _git_diff for the full note.
    return subprocess.run(
        args,
        input=stdin_text,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def run_preflight() -> tuple[bool, str]:
    """Returns (ok, human-readable message). Never raises."""
    # 1. Is the binary even on PATH?
    if shutil.which("claude") is None:
        return False, (
            "Claude Code CLI not found on PATH.\n"
            "Install it first — see https://docs.claude.com/en/docs/claude-code/overview — "
            "then log in by running `claude` once interactively."
        )

    # 2. Version check
    try:
        r = _run(["claude", "--version"], timeout=30)
    except subprocess.TimeoutExpired:
        return False, "`claude --version` timed out after 30s."
    if r.returncode != 0:
        return False, f"`claude --version` failed:\n{(r.stderr or r.stdout).strip()[-500:]}"
    version = (r.stdout or "").strip()

    # 3. Authenticated end-to-end echo test (text-only, single turn)
    args = ["claude", "-p", "reply with the single word ok", "--tools", "", "--max-turns", "1"]
    try:
        r = _run(args, stdin_text="hi\n", timeout=ECHO_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"Echo test timed out after {ECHO_TIMEOUT}s. Is the CLI waiting for a login prompt? Run `claude` interactively once."

    if r.returncode != 0:
        err = (r.stderr or r.stdout).strip()[-500:]
        # Flag-compatibility fallback: older/newer CLIs may not know --tools
        if "--tools" in err or "unknown option" in err.lower() or "unrecognized" in err.lower():
            try:
                r2 = _run(["claude", "-p", "reply with the single word ok", "--max-turns", "1"],
                          stdin_text="hi\n", timeout=ECHO_TIMEOUT)
            except subprocess.TimeoutExpired:
                return False, f"Echo test (fallback) timed out after {ECHO_TIMEOUT}s."
            if r2.returncode == 0:
                return True, (
                    f"PASS with a warning — {version}\n"
                    "Your CLI version rejected the `--tools` flag. Run `claude --help` and update "
                    "the flag assembly in agents/runner.py to your version's equivalent."
                )
            err = (r2.stderr or r2.stdout).strip()[-500:]
        return False, (
            "Claude CLI is installed but the echo test failed (likely not logged in).\n"
            f"CLI said:\n{err}"
        )

    reply = (r.stdout or "").strip()
    return True, f"PASS — {version} — echo reply: {reply[:80]!r}"


if __name__ == "__main__":
    ok, msg = run_preflight()
    print(("PASS\n" if ok else "FAIL\n") + msg if not msg.startswith("PASS") else msg)
    sys.exit(0 if ok else 1)
