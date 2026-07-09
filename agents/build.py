"""Phase 2: the build pipeline (Architect -> Coder -> Reviewer -> Tester).

Headless and bus-first, same shape as agents/debate.py: run_build() takes an
EventBus + run_id so Stage 4's FastAPI backend can drive the exact same
function a browser-triggered run uses. Reads an *existing* agreed_spec.md
from output/<run-id>/ (written by a prior run_debate() call) rather than
taking spec text directly.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import config
from agents.events import AgentEvent, EventBus
from agents.runner import AgentError, run_agent_streaming

_RUN_RESULTS_RE = re.compile(
    r"##\s*Run Results\s*\n+PASSED:\s*(\d+),\s*FAILED:\s*(\d+),\s*ERRORS:\s*(\d+)",
    re.IGNORECASE,
)


def _parse_test_results(tests_text: Optional[str]) -> tuple[Optional[bool], Optional[str]]:
    """Best-effort extraction of Tester's '## Run Results' section (Test Execution addon,
    only populated when allow_exec=True) into (tests_passed, test_summary). Returns
    (None, None) when the section is missing/unparseable -- e.g. allow_exec=False, Tester
    reported "Could not execute", or Tester's stdout didn't follow the format -- rather
    than raising. This is a display convenience, not a strict contract on Tester's output."""
    if not tests_text:
        return None, None
    match = _RUN_RESULTS_RE.search(tests_text)
    if not match:
        return None, None
    passed, failed, errors = (int(x) for x in match.groups())
    summary = f"PASSED: {passed}, FAILED: {failed}, ERRORS: {errors}"
    return (failed == 0 and errors == 0), summary


def _walk_build_dir(build_dir: Path) -> list[str]:
    """Recursive relative-path listing of every file under build_dir, sorted for
    determinism. Empty list if the directory doesn't exist or has no files."""
    if not build_dir.is_dir():
        return []
    return sorted(
        str(p.relative_to(build_dir)).replace("\\", "/")
        for p in build_dir.rglob("*")
        if p.is_file()
    )


def _hash_files(build_dir: Path) -> dict[str, str]:
    """Codebase Analysis Mode (target_mode), no-git fallback: content hash of every file
    under build_dir, keyed by relative path. md5 here is a cheap change-detection
    fingerprint, not a security use -- same convention agents/sandbox.py's own
    verification already used for its "target_path untouched" checksum."""
    return {rel: hashlib.md5((build_dir / rel).read_bytes()).hexdigest() for rel in _walk_build_dir(build_dir)}


def _changed_files(pre: dict[str, str], post: dict[str, str]) -> list[str]:
    """Files added, removed, or content-modified between two _hash_files() snapshots."""
    return sorted(f for f in set(pre) | set(post) if pre.get(f) != post.get(f))


def _git_diff(build_dir: Path) -> tuple[str, list[str]]:
    """Codebase Analysis Mode (target_mode), diff_available path: the cumulative diff
    against the sandbox's baseline commit (see agents/sandbox.py's _init_baseline_commit),
    plus the sorted list of touched files. `git add -A` first so brand-new files show up
    as additions in `git diff --cached` -- a plain `git diff` only covers already-tracked
    files and would silently miss anything the Coder created from scratch. Safe to call
    more than once in the same build (e.g. once after Coder, again after Tester); each
    call reflects the cumulative working-tree state at that moment."""
    subprocess.run(["git", "add", "-A"], cwd=build_dir, capture_output=True, text=True)
    diff = subprocess.run(["git", "diff", "--cached"], cwd=build_dir, capture_output=True, text=True)
    names = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=build_dir, capture_output=True, text=True
    )
    files = sorted(f for f in names.stdout.splitlines() if f.strip())
    return diff.stdout, files


async def _run_step(
    bus: EventBus,
    *,
    agent: str,
    system_prompt_file: str,
    stdin_text: str,
    instruction: str,
    mode: str,
    cost_state: list[float],
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    max_turns: Optional[int] = None,
) -> tuple[Optional[str], float, bool]:
    """Run one build step, streaming deltas onto the bus. Retries once on AgentError;
    on a second failure, emits an `error` event and returns (None, 0.0, False) so the
    caller can decide how the pipeline continues. Mirrors debate.py's _run_turn, minus
    the per-round concept and plus `cwd` for builder-mode steps.

    The third element of the return tuple is `truncated`: True if the CLI hit its
    turn limit but had already produced usable output (see agents/runner.py) -- the
    step is treated as complete, but the caller may want to flag it as a warning.

    `cost_state` is a single-element list shared across the whole run (potentially
    across recon/debate/build phases -- see main.py's _run_pipeline), mutated in place
    so this step's cost is folded into the running total before agent_done/error is
    emitted -- see debate.py's _run_turn for the identical rationale."""
    for attempt in (1, 2):
        bus.emit(AgentEvent(type="agent_start", phase="build", agent=agent))
        try:
            full_text: Optional[str] = None
            cost_usd = 0.0
            truncated = False
            async for event in run_agent_streaming(
                system_prompt_file=system_prompt_file,
                stdin_text=stdin_text,
                instruction=instruction,
                mode=mode,
                agent=agent,
                phase="build",
                cwd=cwd,
                timeout=timeout,
                model=model,
                effort=effort,
                max_turns=max_turns,
            ):
                if event.type == "delta":
                    bus.emit(event)
                elif event.type in ("paused", "resumed"):
                    # Usage-Limit Resilience addon: run_agent_streaming is waiting out a
                    # usage-limit exhaustion internally and will retry the same call once
                    # it's over -- forward these straight through so the UI can show a
                    # paused state instead of the step looking silently stuck.
                    bus.emit(event)
                elif event.type == "result":
                    full_text = event.content
                    cost_usd = event.cost_usd or 0.0
                    truncated = event.truncated
            cost_state[0] += cost_usd
            done_content = "hit the turn limit; using the output produced so far" if truncated else ""
            bus.emit(
                AgentEvent(
                    type="agent_done",
                    phase="build",
                    agent=agent,
                    content=done_content,
                    cost_usd=cost_state[0],
                )
            )
            return full_text, cost_usd, truncated
        except AgentError as e:
            if attempt == 1:
                continue
            tail = str(e).strip()[-500:]
            bus.emit(
                AgentEvent(type="error", phase="build", agent=agent, content=tail, cost_usd=cost_state[0])
            )
            return None, 0.0, False

    return None, 0.0, False  # unreachable, keeps type checkers happy


async def run_build(
    *,
    run_id: str,
    bus: EventBus,
    output_dir: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    target_mode: bool = False,
    diff_available: bool = False,
    cost_state: Optional[list[float]] = None,
    allow_exec: bool = False,
) -> dict:
    """Run the full build pipeline against an existing debate run. Returns a summary dict.

    Architect and Coder failures are terminal (nothing downstream can proceed without
    them); a Reviewer failure is not -- the Tester still runs, using a system-note
    fallback in place of review.md.

    `model`/`effort` are optional per-run overrides (e.g. from the browser UI) applied to
    all four steps; omitted, they fall back to config.py's BUILD_MODEL default via
    agents/runner.py.

    `target_mode` is Codebase Analysis Mode's hook (SPEC.md Stage 10): when True,
    `build_dir` is expected to already be a populated sandbox (agents/sandbox.py, run
    before this) rather than a fresh empty directory -- no scaffolding happens here. The
    Coder swaps to `config.CODER_PATCH_PROMPT_FILE` (edit-in-place persona), and the old
    "did build/ end up non-empty?" guard is replaced by "did anything actually change?":
    an empty diff after the Coder's turn is treated the same way empty-build-dir was
    before -- error event, Reviewer/Tester skipped. Reviewer and Tester run completely
    unchanged in target_mode; their prompts don't differ between a fresh tree and a
    patched existing one (SPEC.md). `diff_available` (from that same prior sandbox-prep
    call) picks the change-detection mechanism: a real `git diff` against the sandbox's
    baseline commit when True, or a before/after file-hash comparison when False (no git
    on PATH) -- the latter can still tell what changed, it just can't produce patch.diff.
    Ignored when target_mode is False.

    `cost_state` lets a caller (main.py's _run_pipeline) share one running-total
    accumulator across multiple phases (e.g. debate -> build) so the frontend's
    cumulative cost display doesn't reset between phases. Defaults to a fresh [0.0]
    for headless/standalone callers that only ever run this one phase.

    `allow_exec` (Test Execution addon, SPEC.md v6): opt-in, default False. When True,
    the Tester runs in "builder_exec" mode (agents/runner.py) instead of plain "builder"
    and actually invokes the test command it documents, once, recording real PASSED/
    FAILED/ERRORS counts in tests.md's new Run Results section (parsed into this
    function's result dict as tests_passed/test_summary, best-effort). When False (the
    default, and every run before this addon), behavior is unchanged: Tester never gets
    a Bash tool, tests.md's Run Results section reports "Could not execute", and
    tests_passed/test_summary come back None."""
    out_dir = Path(output_dir) if output_dir else Path(config.OUTPUT_DIR) / run_id
    spec_path = out_dir / "agreed_spec.md"
    if not spec_path.is_file():
        raise AgentError(f"agreed_spec.md not found at {spec_path} -- run the debate phase first")
    spec_text = spec_path.read_text(encoding="utf-8")
    if cost_state is None:
        cost_state = [0.0]

    result = {
        "run_id": run_id,
        "output_dir": str(out_dir),
        "architecture_path": None,
        "build_dir": None,
        "review_path": None,
        "tests_path": None,
        "patch_diff_path": None,
        "build_ok": False,
        "total_cost_usd": 0.0,
        "warnings": [],
        "tests_passed": None,
        "test_summary": None,
    }

    # --- Architect ---
    arch_prompt_file, arch_mode, arch_timeout = config.BUILD_AGENTS["Architect"]
    arch_instruction = (
        "Read the agreed spec on stdin and produce the architecture document now, "
        "following your role and output-format rules exactly."
    )
    arch_text, _arch_cost, arch_truncated = await _run_step(
        bus,
        agent="Architect",
        system_prompt_file=arch_prompt_file,
        stdin_text=spec_text,
        instruction=arch_instruction,
        mode=arch_mode,
        cost_state=cost_state,
        timeout=arch_timeout,
        model=model,
        effort=effort,
    )
    result["total_cost_usd"] = cost_state[0]
    if arch_truncated:
        result["warnings"].append("Architect hit the turn limit; using the output produced so far.")

    if arch_text is None:
        bus.emit(
            AgentEvent(
                type="phase_done",
                phase="build",
                content=json.dumps({**result, "reason": "architect_failed"}),
                cost_usd=cost_state[0],
            )
        )
        return result

    architecture_path = out_dir / "architecture.md"
    architecture_path.write_text(arch_text, encoding="utf-8")
    result["architecture_path"] = str(architecture_path)

    # --- Coder ---
    build_dir = out_dir / "build"
    pre_snapshot: Optional[dict[str, str]] = None
    if target_mode:
        if not build_dir.is_dir():
            raise AgentError(
                f"target_mode requires an already-populated sandbox at {build_dir} -- "
                "run sandbox prep (agents/sandbox.py) first"
            )
        if not diff_available:
            pre_snapshot = await asyncio.to_thread(_hash_files, build_dir)
    else:
        build_dir.mkdir(parents=True, exist_ok=True)
    result["build_dir"] = str(build_dir)

    coder_prompt_file, coder_mode, coder_timeout = config.BUILD_AGENTS["Coder"]
    if target_mode:
        coder_prompt_file = config.CODER_PATCH_PROMPT_FILE
    coder_stdin = (
        "=== AGREED SPEC ===\n"
        + spec_text.strip()
        + "\n\n=== ARCHITECTURE ===\n"
        + arch_text.strip()
        + "\n"
    )
    if target_mode:
        coder_instruction = (
            "Read the agreed spec and architecture blueprint on stdin, then read the relevant "
            "existing files in the current working directory and make the smallest correct "
            "edit(s) that satisfy the File Plan -- prefer editing over creating, and do not "
            "touch anything outside the spec's scope. Follow your stdout-format rules exactly."
        )
    else:
        coder_instruction = (
            "Read the agreed spec and architecture blueprint on stdin, then create the files "
            "directly in the current working directory following the File Tree and Build Order. "
            "Follow your stdout-format rules exactly."
        )
    _, _coder_cost, coder_truncated = await _run_step(
        bus,
        agent="Coder",
        system_prompt_file=coder_prompt_file,
        stdin_text=coder_stdin,
        instruction=coder_instruction,
        mode=coder_mode,
        cost_state=cost_state,
        cwd=str(build_dir),
        timeout=coder_timeout,
        model=model,
        effort=effort,
    )
    result["total_cost_usd"] = cost_state[0]
    if coder_truncated:
        result["warnings"].append("Coder hit the turn limit; using the output produced so far.")

    if target_mode:
        if diff_available:
            _, files = await asyncio.to_thread(_git_diff, build_dir)
        else:
            post_snapshot = await asyncio.to_thread(_hash_files, build_dir)
            files = _changed_files(pre_snapshot, post_snapshot)
            result["warnings"].append(
                "git unavailable for this sandbox; change detection used a file-hash "
                "comparison instead, and no patch.diff will be produced this run."
            )
    else:
        files = _walk_build_dir(build_dir)
    build_ok = bool(files)
    result["build_ok"] = build_ok

    if not build_ok:
        reason = "coder_made_no_changes" if target_mode else "coder_produced_nothing"
        message = (
            "Coder made no changes to the sandbox; skipping Reviewer and Tester."
            if target_mode
            else "Coder produced no files in build/; skipping Reviewer and Tester."
        )
        bus.emit(
            AgentEvent(type="error", phase="build", agent="Coder", content=message, cost_usd=cost_state[0])
        )
        bus.emit(
            AgentEvent(
                type="phase_done",
                phase="build",
                content=json.dumps({**result, "reason": reason}),
                cost_usd=cost_state[0],
            )
        )
        return result

    bus.emit(AgentEvent(type="files_updated", phase="build", content=json.dumps(files)))

    # --- Reviewer ---
    reviewer_prompt_file, reviewer_mode, reviewer_timeout = config.BUILD_AGENTS["Reviewer"]
    reviewer_instruction = (
        "Read the agreed spec on stdin and the code in the current working directory, "
        "then produce the review document now, following your output-format rules exactly."
    )
    review_text, _review_cost, review_truncated = await _run_step(
        bus,
        agent="Reviewer",
        system_prompt_file=reviewer_prompt_file,
        stdin_text=spec_text,
        instruction=reviewer_instruction,
        mode=reviewer_mode,
        cost_state=cost_state,
        cwd=str(build_dir),
        timeout=reviewer_timeout,
        model=model,
        effort=effort,
    )
    result["total_cost_usd"] = cost_state[0]
    if review_truncated:
        result["warnings"].append("Reviewer hit the turn limit; using the output produced so far.")

    review_path: Optional[Path] = None
    if review_text is not None:
        review_path = out_dir / "review.md"
        review_path.write_text(review_text, encoding="utf-8")
        result["review_path"] = str(review_path)
        tester_stdin = review_text
    else:
        # Reviewer failing isn't terminal -- the Tester can still work directly off the
        # code, it just loses the review's map of where the code is fragile.
        result["review_path"] = None
        tester_stdin = (
            "Reviewer was unavailable this run; no review.md was produced. Use your own "
            "judgment: read the source code directly and write tests for it."
        )

    # --- Tester ---
    tester_prompt_file, tester_mode, tester_timeout = config.BUILD_AGENTS["Tester"]
    tester_max_turns: Optional[int] = None
    if allow_exec:
        # Test Execution addon (SPEC.md v6), opt-in only -- "builder" (registry default)
        # never gets a Bash tool at all, so this only takes effect when a run explicitly
        # asks for it. tester.txt's own instructions are mode-agnostic (it checks whether
        # a Bash tool is actually available to it), so no prompt-file swap is needed here.
        tester_mode = "builder_exec"
        tester_max_turns = config.TESTER_MAX_TURNS
    tester_instruction = (
        "Read the review on stdin (or use your own judgment if none was produced) and the "
        "code in the current working directory, then create the test files directly and "
        "follow your stdout-format rules exactly."
    )
    tests_text, _tester_cost, tester_truncated = await _run_step(
        bus,
        agent="Tester",
        system_prompt_file=tester_prompt_file,
        stdin_text=tester_stdin,
        instruction=tester_instruction,
        mode=tester_mode,
        cost_state=cost_state,
        cwd=str(build_dir),
        timeout=tester_timeout,
        model=model,
        effort=effort,
        max_turns=tester_max_turns,
    )
    result["total_cost_usd"] = cost_state[0]
    if tester_truncated:
        result["warnings"].append("Tester hit the turn limit; using the output produced so far.")

    if tests_text is not None:
        tests_path = out_dir / "tests.md"
        tests_path.write_text(tests_text, encoding="utf-8")
        result["tests_path"] = str(tests_path)
        result["tests_passed"], result["test_summary"] = _parse_test_results(tests_text)
    else:
        result["tests_path"] = None

    # Final files_updated: recomputed, not the Coder-step snapshot, since Reviewer/Tester
    # may have added/changed files too (e.g. Tester's own test files). In target_mode this
    # is the touched-files-only list (not the whole sandbox tree -- a real target codebase
    # can have hundreds of unrelated files, and dumping all of them into the UI's file
    # panel on every files_updated would be noise); a real target_mode also writes
    # patch.diff here, once, covering the whole build phase's cumulative changes.
    if target_mode:
        if diff_available:
            diff_text, final_files = await asyncio.to_thread(_git_diff, build_dir)
            if diff_text:
                patch_path = out_dir / "patch.diff"
                patch_path.write_text(diff_text, encoding="utf-8")
                result["patch_diff_path"] = str(patch_path)
        else:
            post_snapshot = await asyncio.to_thread(_hash_files, build_dir)
            final_files = _changed_files(pre_snapshot, post_snapshot)
        bus.emit(AgentEvent(type="files_updated", phase="build", content=json.dumps(final_files)))
    else:
        bus.emit(
            AgentEvent(type="files_updated", phase="build", content=json.dumps(_walk_build_dir(build_dir)))
        )
    bus.emit(
        AgentEvent(type="phase_done", phase="build", content=json.dumps(result), cost_usd=cost_state[0])
    )

    return result


async def _headless_main(run_id: str, target_mode: bool = False, allow_exec: bool = False) -> None:
    bus = EventBus()

    diff_available = False
    if target_mode:
        # Standalone headless test convenience: infer diff_available from whether the
        # sandbox (agents/sandbox.py) actually got a baseline commit, rather than
        # requiring a separate flag here. Stage 11's real orchestration will instead pass
        # the value straight through from that earlier prepare_sandbox() call.
        build_dir = Path(config.OUTPUT_DIR) / run_id / "build"
        diff_available = (build_dir / ".git").is_dir()

    async def _consume() -> None:
        async for ev in bus.stream():
            if ev.type == "agent_start":
                print(f"\n=== {ev.agent} ===", flush=True)
            elif ev.type == "delta":
                print(ev.content, end="", flush=True)
            elif ev.type == "agent_done":
                print(flush=True)
            elif ev.type == "error":
                print(f"\n[ERROR] {ev.agent}: {ev.content}", file=sys.stderr, flush=True)
            elif ev.type == "files_updated":
                files = json.loads(ev.content)
                print(f"\n--- files_updated: {len(files)} file(s) ---", flush=True)
            elif ev.type == "phase_done":
                print(f"\n--- phase done: {ev.content}", flush=True)

    consumer_task = asyncio.create_task(_consume())
    try:
        result = await run_build(
            run_id=run_id,
            bus=bus,
            target_mode=target_mode,
            diff_available=diff_available,
            allow_exec=allow_exec,
        )
    except AgentError as e:
        bus.close()
        await consumer_task
        print(f"\n[AgentError] {e}", file=sys.stderr)
        sys.exit(1)
    bus.close()
    await consumer_task

    print(f"\nrun_id: {result['run_id']}")
    print(f"architecture: {result['architecture_path']}")
    print(f"build_dir: {result['build_dir']} (build_ok={result['build_ok']})")
    print(f"review: {result['review_path']}")
    print(f"tests: {result['tests_path']}")
    if result.get("test_summary"):
        print(f"test_summary: {result['test_summary']} (tests_passed={result['tests_passed']})")
    print(f"patch_diff: {result['patch_diff_path']}")
    print(f"total_cost_usd: ${result['total_cost_usd']:.4f}")
    if result.get("warnings"):
        print("warnings:")
        for w in result["warnings"]:
            print(f"  - {w}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the Phase 2 build pipeline headlessly against an existing debate run."
    )
    parser.add_argument("run_id", help="run id of an existing output/<run-id>/ containing agreed_spec.md")
    parser.add_argument(
        "--target-mode",
        action="store_true",
        help="Codebase Analysis Mode: build_dir is an already-populated sandbox (agents/sandbox.py) "
        "to patch in place, not a fresh empty directory to scaffold",
    )
    parser.add_argument(
        "--allow-exec",
        action="store_true",
        help="Test Execution addon: let the Tester actually run the tests it writes (builder_exec "
        "mode, opt-in, default off)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    # Model output is arbitrary Unicode; a Windows console's legacy codepage (e.g.
    # cp1252) can't encode all of it and print() would crash. See agents/debate.py.
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = _parse_args(sys.argv[1:])
    asyncio.run(_headless_main(args.run_id, target_mode=args.target_mode, allow_exec=args.allow_exec))
