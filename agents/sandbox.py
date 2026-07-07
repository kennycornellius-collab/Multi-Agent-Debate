"""Codebase Analysis Mode (SPEC.md addon), Stage 7: sandbox preparation ("Step 0").

Copies an existing codebase into output/<run-id>/build/ so every later agent (Recon,
Critic, Coder, Reviewer, Tester -- Stages 8-10) works against a disposable copy and
the user's real target_path is never written to. Plain filesystem + git operations
only, no CLI/agent calls -- headless and bus-first like agents/debate.py and
agents/build.py, so Stage 11's FastAPI backend can drive the exact same function a
browser-triggered run uses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

import config
from agents.events import AgentEvent, EventBus


def _init_baseline_commit(build_dir: Path) -> tuple[bool, bool, Optional[str]]:
    """Best-effort: git-init + one baseline commit inside build_dir, using an inline
    author identity and with signing/hooks disabled for this single internal commit
    (user-authorized exception -- this repo is a hidden diff tool inside our own
    output dir, never the user's, never pushed anywhere).

    Returns (diff_available, baseline_committed, warning). Never raises -- a git
    hiccup costs the run its patch.diff later, not the whole sandbox.
    """
    if shutil.which("git") is None:
        return False, False, "git not found on PATH; patch.diff will be unavailable this run"

    def _run(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(args, cwd=build_dir, capture_output=True, text=True)

    steps = [
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        [
            "git",
            "-c", f"user.name={config.SANDBOX_GIT_NAME}",
            "-c", f"user.email={config.SANDBOX_GIT_EMAIL}",
            "-c", "commit.gpgsign=false",
            "commit", "--no-verify", "-q", "-m", "baseline",
        ],
    ]
    for args in steps:
        r = _run(args)
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip()[-300:]
            return False, False, f"git baseline commit failed ({args[0]} {args[1]}): {tail}"

    return True, True, None


async def prepare_sandbox(
    *,
    target_path: str,
    run_id: str,
    bus: EventBus,
    output_dir: Optional[str] = None,
) -> dict:
    """Copy target_path into output/<run-id>/build/ (or output_dir/build if given),
    excluding config.SANDBOX_IGNORE entries, then attempt a throwaway git baseline
    commit inside the copy for later diffing (Stage 10).

    Returns a result dict; `ok` is False for every terminal failure (bad/missing
    target, dest would be inside target, copy failed, or nothing was copied). A
    missing/broken git is NOT terminal -- diff_available/baseline_committed just
    come back False with a warning, and the sandbox is still usable.
    """
    result = {
        "run_id": run_id,
        "output_dir": None,
        "build_dir": None,
        "target_path": target_path,
        "files_copied": 0,
        "diff_available": False,
        "baseline_committed": False,
        "ok": False,
        "warnings": [],
    }

    src = Path(target_path).expanduser().resolve()
    if not src.is_dir():
        msg = f"target_path does not exist or is not a directory: {src}"
        bus.emit(AgentEvent(type="error", phase="sandbox", agent="system", content=msg))
        bus.emit(
            AgentEvent(
                type="phase_done",
                phase="sandbox",
                content=json.dumps({**result, "reason": "bad_target_path"}),
            )
        )
        return result

    result["target_path"] = str(src)

    out_dir = Path(output_dir) if output_dir else Path(config.OUTPUT_DIR) / run_id
    build_dir = out_dir / "build"
    build_dir_resolved = build_dir.resolve()
    result["output_dir"] = str(out_dir)
    result["build_dir"] = str(build_dir)

    # target_path may itself contain this project's own output/ dir (true whenever
    # someone points codebase mode at this very repo) -- copying src into a
    # destination that lives *inside* src would mean copytree tries to copy a
    # directory into itself. Must be checked before any directory gets created.
    if build_dir_resolved == src or build_dir_resolved.is_relative_to(src):
        msg = (
            f"the sandbox directory ({build_dir_resolved}) would be inside target_path "
            f"({src}) -- refusing to copy a directory into itself"
        )
        bus.emit(AgentEvent(type="error", phase="sandbox", agent="system", content=msg))
        bus.emit(
            AgentEvent(
                type="phase_done",
                phase="sandbox",
                content=json.dumps({**result, "reason": "dest_inside_target"}),
            )
        )
        return result

    out_dir.mkdir(parents=True, exist_ok=True)

    bus.emit(
        AgentEvent(
            type="agent_start", phase="sandbox", agent="system", content=f"Copying {src} into sandbox..."
        )
    )

    try:
        await asyncio.to_thread(
            shutil.copytree, src, build_dir, ignore=shutil.ignore_patterns(*config.SANDBOX_IGNORE)
        )
    except OSError as e:
        shutil.rmtree(build_dir, ignore_errors=True)
        msg = f"failed to copy target_path into sandbox: {e}"
        bus.emit(AgentEvent(type="error", phase="sandbox", agent="system", content=msg))
        bus.emit(
            AgentEvent(
                type="phase_done", phase="sandbox", content=json.dumps({**result, "reason": "copy_failed"})
            )
        )
        return result

    files_copied = sum(1 for p in build_dir.rglob("*") if p.is_file())
    result["files_copied"] = files_copied
    if files_copied == 0:
        shutil.rmtree(build_dir, ignore_errors=True)
        msg = "target_path contains no files to analyze after applying the ignore list"
        bus.emit(AgentEvent(type="error", phase="sandbox", agent="system", content=msg))
        bus.emit(
            AgentEvent(
                type="phase_done", phase="sandbox", content=json.dumps({**result, "reason": "empty_target"})
            )
        )
        return result

    diff_available, baseline_committed, warning = await asyncio.to_thread(_init_baseline_commit, build_dir)
    result["diff_available"] = diff_available
    result["baseline_committed"] = baseline_committed
    if warning:
        result["warnings"].append(warning)

    result["ok"] = True
    done_content = f"Copied {files_copied} file(s)"
    done_content += "; git baseline committed" if baseline_committed else "; diff unavailable this run"
    bus.emit(AgentEvent(type="agent_done", phase="sandbox", agent="system", content=done_content))
    bus.emit(AgentEvent(type="phase_done", phase="sandbox", content=json.dumps(result)))
    return result


async def _headless_main(target_path: str) -> None:
    run_id = uuid.uuid4().hex[:8]
    bus = EventBus()

    async def _consume() -> None:
        async for ev in bus.stream():
            if ev.type == "agent_start":
                print(f"\n=== sandbox ===\n{ev.content}", flush=True)
            elif ev.type == "agent_done":
                print(ev.content, flush=True)
            elif ev.type == "error":
                print(f"\n[ERROR] {ev.content}", file=sys.stderr, flush=True)
            elif ev.type == "phase_done":
                print(f"\n--- phase done: {ev.content}", flush=True)

    consumer_task = asyncio.create_task(_consume())
    result = await prepare_sandbox(target_path=target_path, run_id=run_id, bus=bus)
    bus.close()
    await consumer_task

    print(f"\nrun_id: {result['run_id']}")
    print(f"build_dir: {result['build_dir']}")
    print(f"files_copied: {result['files_copied']}")
    print(f"diff_available: {result['diff_available']}")
    if result.get("warnings"):
        print("warnings:")
        for w in result["warnings"]:
            print(f"  - {w}")
    if not result["ok"]:
        sys.exit(1)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 7: sandbox preparation only, headless (Codebase Analysis Mode)."
    )
    parser.add_argument("target_path", help="path to the existing codebase to copy into a sandbox")
    return parser.parse_args(argv)


if __name__ == "__main__":
    # Model output isn't involved in this stage, but paths/warnings can still contain
    # non-cp1252 characters on a Windows console -- see agents/debate.py's identical guard.
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = _parse_args(sys.argv[1:])
    asyncio.run(_headless_main(args.target_path))
