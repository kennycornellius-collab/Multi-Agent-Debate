"""The ONLY module in this codebase that shells out to the `claude` CLI.

Every other module calls `run_agent_streaming()` or `run_agent()` from here.
No `anthropic` SDK import, no `api.anthropic.com`, no `ANTHROPIC_API_KEY` --
authentication is entirely the user's existing `claude` CLI login.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import AsyncIterator, Literal, Optional

import config
from agents.events import AgentEvent

CLAUDE_BIN = shutil.which("claude")

STDERR_TAIL_CHARS = 500


class AgentError(Exception):
    """Raised when an agent invocation fails (nonzero exit, timeout, bad setup).

    retryable=False marks deterministic failures -- retrying the identical call is
    guaranteed to fail the same way, so the callers' retry-once loops
    (debate._run_turn / build._run_step) would just double the cost and wall-clock to
    reach the same abort (audit M5): a --max-budget-usd exhaustion, a nonexistent
    model, a missing prompt file, a missing CLI binary. Timeouts and generic nonzero
    exits stay retryable=True -- those can genuinely be transient.
    """

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class RateLimitError(AgentError):
    """Raised instead of a plain AgentError (Usage-Limit Resilience addon) when a failure
    looks like a subscription usage-limit exhaustion, or (is_transient_429=True) a
    transient API-tier 429 -- as opposed to a genuine model/CLI error, which still raises
    a plain AgentError unchanged. Caught internally by run_agent_streaming's wait/resume
    loop; only escapes to a caller that passed wait_on_rate_limit=False."""

    def __init__(self, message: str, *, retry_at: Optional[datetime], is_transient_429: bool):
        super().__init__(message)
        self.retry_at = retry_at
        self.is_transient_429 = is_transient_429


def _resolve_prompt_file(system_prompt_file: str) -> Path:
    path = Path(config.PROMPTS_DIR) / system_prompt_file
    if not path.is_file():
        raise AgentError(f"system prompt file not found: {path}", retryable=False)
    return path.resolve()


def _build_args(
    *,
    prompt_path: Path,
    instruction: str,
    mode: Literal["text_only", "builder", "read_only", "builder_exec"],
    include_budget_flag: bool,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    max_turns: Optional[int] = None,
) -> list[str]:
    args = [
        "-p",
        instruction,
        "--append-system-prompt-file",
        str(prompt_path),
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]

    if mode == "text_only":
        args += ["--tools", "", "--max-turns", "1"]
        default_model = config.DEBATE_MODEL
    elif mode == "builder":
        # Explicit --tools (no Bash) rather than relying on the CLI's "all built-in
        # tools" default: --permission-mode acceptEdits auto-approves every tool call,
        # not just Edit/Write, so omitting --tools here would silently hand Coder/
        # Reviewer a real, unrestricted shell -- confirmed empirically (a call with
        # acceptEdits and no --tools restriction ran an arbitrary Bash command with zero
        # denial), not just assumed. File tools only, by construction.
        args += [
            "--tools", "Read,Edit,Write",
            "--permission-mode", "acceptEdits",
            "--max-turns", str(max_turns or config.BUILD_MAX_TURNS),
        ]
        default_model = config.BUILD_MODEL
    elif mode == "read_only":
        # Structural enforcement, not just prompt discipline: Read/Glob/Grep only, no
        # Edit/Write/Bash -- confirmed against the real CLI that this needs no
        # --permission-mode flag (there's nothing to grant permission for) and doesn't
        # hang waiting on a prompt it can never receive non-interactively.
        args += ["--tools", "Read,Glob,Grep", "--max-turns", str(max_turns or config.RECON_MAX_TURNS)]
        default_model = config.RECON_MODEL
    elif mode == "builder_exec":
        # Test Execution + BugFixer Agent addon (SPEC.md v6): "builder" plus a real Bash
        # grant. Confirmed empirically this needs all three pieces, not just acceptEdits:
        # acceptEdits alone auto-approves *some* Bash commands via an internal risk
        # classifier (trivial/read-only-looking ones), but denies commands that look
        # consequential (pip install, running a test suite) with no one able to approve
        # them non-interactively -- --allowedTools pre-approves exactly those past the
        # classifier (config.BUILD_EXEC_ALLOWED_TOOLS, a curated cross-ecosystem list of
        # install/test-runner invocations). --disallowedTools then hard-blocks known-
        # dangerous prefixes (config.BUILD_EXEC_DISALLOWED_TOOLS) regardless of the
        # above -- confirmed with a real call in the same session as an allowed command.
        # Still not a hard sandbox: anything not on the denylist and not requiring
        # classifier approval can run. See SPEC.md's Test Execution addon for the full
        # empirical trail and the other mitigations (cwd confinement, tight max-turns,
        # prompt-level restraint) this relies on alongside these flags.
        args += [
            "--tools", "Read,Edit,Write,Bash",
            "--permission-mode", "acceptEdits",
            "--allowedTools", " ".join(config.BUILD_EXEC_ALLOWED_TOOLS),
            "--disallowedTools", " ".join(config.BUILD_EXEC_DISALLOWED_TOOLS),
            "--max-turns", str(max_turns or config.BUILD_MAX_TURNS),
        ]
        default_model = config.BUILD_MODEL
    else:
        raise AgentError(f"unknown mode: {mode!r}", retryable=False)

    # An explicit per-call override (e.g. from the browser UI) wins over config.py's
    # per-mode default; config.py's None-means-CLI-default behavior is unchanged for
    # any caller that doesn't pass one.
    resolved_model = model or default_model
    if resolved_model:
        args += ["--model", resolved_model]

    if effort:
        args += ["--effort", effort]

    if include_budget_flag:
        args += ["--max-budget-usd", str(config.MAX_BUDGET_USD_PER_CALL)]

    return args


def _looks_like_unknown_flag_error(stderr: str) -> bool:
    lowered = stderr.lower()
    return "--max-budget-usd" in stderr or "unknown option" in lowered or "unrecognized" in lowered


_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Only the CLI's message *wording* is documented (e.g. "resets 3:45pm" / "resets Mon
# 12:00am"), not a machine-readable reset field -- see config.py's RATE_LIMIT_MARKERS
# comment. Deliberately tolerant (optional weekday, single regex) rather than a strict
# format, since this is a best-effort parse with a safe fallback (config.RATE_LIMIT_POLL_
# SECONDS) when it doesn't match.
_RESET_TIME_RE = re.compile(
    r"resets?\s+(?:(mon|tue|wed|thu|fri|sat|sun)\w*\s+)?(\d{1,2}):(\d{2})\s*([ap]m)",
    re.IGNORECASE,
)


def _parse_reset_time(text: str, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """Best-effort parse of a "... resets <time>" / "... resets <Weekday> <time>" fragment
    into a concrete datetime to wait until. Returns None if nothing recognizable is found --
    callers fall back to a fixed poll interval instead of guessing."""
    match = _RESET_TIME_RE.search(text)
    if not match:
        return None

    weekday_str, hour_str, minute_str, meridiem = match.groups()
    now = now or datetime.now()

    hour = int(hour_str) % 12
    if meridiem.lower() == "pm":
        hour += 12
    minute = int(minute_str)

    if weekday_str:
        target_weekday = _WEEKDAYS.index(weekday_str.lower()[:3])
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (target_weekday - now.weekday()) % 7
        candidate += timedelta(days=days_ahead)
        if days_ahead == 0 and candidate <= now:
            candidate += timedelta(days=7)
        return candidate

    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _detect_rate_limit(
    *, result_text: Optional[str], stderr_text: str, rate_limit_payload: Optional[dict]
) -> Optional[RateLimitError]:
    """Scan every text source a usage-limit message could plausibly arrive in -- the
    exact NDJSON envelope for this isn't documented, so this checks the terminal result
    event's text, stderr, and the (previously discarded) rate_limit_event payload all at
    once -- and, on a match against config.RATE_LIMIT_MARKERS, return a RateLimitError
    describing what was found instead of a plain AgentError. Returns None when nothing
    matches, leaving the caller's ordinary AgentError path unchanged.

    Logs the raw matched text loudly on a hit: the marker list is a best guess at the
    CLI's real wording (confirmed docs only, never triggered against a genuinely
    exhausted account -- see progress.md), so if the wording ever drifts, this is the
    trail that makes it diagnosable instead of silently missed."""
    candidates = [result_text or "", stderr_text or ""]
    if rate_limit_payload:
        candidates.append(json.dumps(rate_limit_payload))
    combined = "\n".join(c for c in candidates if c)
    lowered = combined.lower()

    for marker in config.RATE_LIMIT_MARKERS:
        if marker in lowered:
            retry_at = _parse_reset_time(combined)
            print(
                f"[runner] detected usage-limit marker {marker!r} in CLI output "
                f"(retry_at={retry_at}): {combined[:300]!r}",
                file=sys.stderr,
            )
            return RateLimitError(combined.strip()[-500:], retry_at=retry_at, is_transient_429=False)

    if config.RATE_LIMIT_TRANSIENT_MARKER in lowered:
        print(
            f"[runner] detected transient rate-limit marker in CLI output: {combined[:300]!r}",
            file=sys.stderr,
        )
        return RateLimitError(combined.strip()[-500:], retry_at=None, is_transient_429=True)

    return None


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill the CLI and any children it spawned. proc.kill() alone can leave
    child processes running on Windows, so shell out to taskkill there."""
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/F", "/T", "/PID", str(proc.pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        proc.kill()
    try:
        await proc.wait()
    except Exception:
        pass


async def _spawn(
    args: list[str], *, cwd: Optional[str], env: Optional[dict[str, str]] = None
) -> asyncio.subprocess.Process:
    if CLAUDE_BIN is None:
        raise AgentError("`claude` CLI not found on PATH", retryable=False)
    return await asyncio.create_subprocess_exec(
        CLAUDE_BIN,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,  # None = inherit; builder_exec passes a venv-activated copy (audit L4)
        limit=config.STREAM_READ_LIMIT_BYTES,
    )


async def _run_attempt(**common_kwargs) -> AsyncIterator[AgentEvent]:
    """One full call attempt: the existing --max-budget-usd guarded-retry-once, with no
    rate-limit handling of its own -- that lives one level up in run_agent_streaming,
    which is the only place that needs to catch RateLimitError and turn it into a wait
    instead of letting it surface as an ordinary AgentError."""
    gen = _run_once(**common_kwargs, include_budget_flag=True)
    try:
        first_event = await gen.__anext__()
    except StopAsyncIteration:
        return
    except AgentError as e:
        if not isinstance(e, RateLimitError) and _looks_like_unknown_flag_error(str(e)):
            print("[runner] --max-budget-usd rejected by this CLI build; retrying without it", file=sys.stderr)
            gen = _run_once(**common_kwargs, include_budget_flag=False)
            first_event = await gen.__anext__()
        else:
            raise

    yield first_event
    async for event in gen:
        yield event


async def _wait_for_quota(
    err: RateLimitError, *, agent: Optional[str], phase: Optional[str], round: Optional[int]
) -> AsyncIterator[AgentEvent]:
    """Usage-Limit Resilience addon. Yields a `paused` heartbeat every
    config.RATE_LIMIT_HEARTBEAT_SECONDS while waiting out a usage-limit exhaustion, then a
    final `resumed` event once the wait is over. Never raises -- run_agent_streaming
    re-runs the identical call once this generator finishes.

    A parsed retry_at governs the wait; a transient 429 gets a short fixed backoff
    (config.API_429_BACKOFF_SECONDS); an unparseable subscription-quota message falls back
    to a fixed poll interval (config.RATE_LIMIT_POLL_SECONDS) -- see _parse_reset_time's
    docstring for why a machine-readable reset time isn't guaranteed to be available."""
    now = datetime.now()
    if err.is_transient_429:
        wait_seconds = float(config.API_429_BACKOFF_SECONDS)
        retry_at = now + timedelta(seconds=wait_seconds)
    elif err.retry_at is not None:
        retry_at = err.retry_at
        wait_seconds = max(0.0, (retry_at - now).total_seconds())
    else:
        wait_seconds = float(config.RATE_LIMIT_POLL_SECONDS)
        retry_at = now + timedelta(seconds=wait_seconds)

    retry_at_iso = retry_at.isoformat()
    reason = str(err).strip() or "usage limit reached"
    yield AgentEvent(
        type="paused", phase=phase, round=round, agent=agent, content=reason, retry_at=retry_at_iso
    )

    remaining = wait_seconds
    while remaining > 0:
        chunk = min(remaining, config.RATE_LIMIT_HEARTBEAT_SECONDS)
        await asyncio.sleep(chunk)
        remaining -= chunk
        if remaining > 0:
            yield AgentEvent(
                type="paused", phase=phase, round=round, agent=agent,
                content=reason, retry_at=retry_at_iso,
            )

    yield AgentEvent(type="resumed", phase=phase, round=round, agent=agent, content="")


async def run_agent_streaming(
    *,
    system_prompt_file: str,
    stdin_text: str,
    instruction: str,
    mode: Literal["text_only", "builder", "read_only", "builder_exec"],
    agent: Optional[str] = None,
    phase: Optional[str] = None,
    round: Optional[int] = None,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    max_turns: Optional[int] = None,
    env: Optional[dict[str, str]] = None,
    wait_on_rate_limit: bool = True,
) -> AsyncIterator[AgentEvent]:
    """Spawn the `claude` CLI for one agent turn and yield AgentEvents as output streams in.

    `env` is an optional full environment for the spawned CLI process (None = inherit).
    The Test Execution addon passes a venv-activated copy (VIRTUAL_ENV + the venv's
    scripts dir prepended to PATH) so every Bash command the agent runs -- pip installs
    above all -- resolves into the run's disposable venv instead of the global
    environment (audit L4). The claude process's own children inherit it transitively.

    Yields `delta` events as text arrives, then a final `result` event carrying the
    full text and reported cost. Raises AgentError on nonzero exit or timeout.

    `model`/`effort` are optional per-call overrides (e.g. from the browser UI); when
    omitted, `_build_args` falls back to config.py's per-mode DEBATE_MODEL/BUILD_MODEL/
    RECON_MODEL default, unchanged from before these params existed.

    `max_turns` is an optional per-call override of the mode's default turn budget --
    added for "builder_exec" (Test Execution addon), which is shared by agents with
    different budgets (Tester's TESTER_MAX_TURNS today, BugFixer's own budget later);
    omitted, each mode falls back to its own config.py default unchanged.

    Guarded --max-budget-usd: if the CLI rejects the flag as unrecognized (older/newer
    builds), retry once without it -- transparently, before any output has been yielded.

    wait_on_rate_limit (Usage-Limit Resilience addon): when True (default), a detected
    subscription usage-limit exhaustion pauses this call -- yielding `paused` heartbeats
    then a `resumed` event (see _wait_for_quota) -- and transparently re-runs the
    identical call once the quota is expected back, instead of raising. Callers that must
    fail fast rather than potentially wait hours (e.g. main.py's POST /models/check) pass
    False, in which case a usage-limit hit surfaces as an ordinary AgentError.
    """
    if mode in ("builder", "read_only", "builder_exec") and not cwd:
        raise AgentError(f"mode={mode!r} requires cwd", retryable=False)

    prompt_path = _resolve_prompt_file(system_prompt_file)
    if timeout is None:
        if mode == "text_only":
            timeout = config.DEBATE_TIMEOUT
        elif mode == "read_only":
            timeout = config.RECON_TIMEOUT
        else:
            timeout = config.CODER_TIMEOUT

    common_kwargs = dict(
        prompt_path=prompt_path,
        stdin_text=stdin_text,
        instruction=instruction,
        mode=mode,
        agent=agent,
        phase=phase,
        round=round,
        cwd=cwd,
        timeout=timeout,
        model=model,
        effort=effort,
        max_turns=max_turns,
        env=env,
    )

    while True:
        try:
            async for event in _run_attempt(**common_kwargs):
                yield event
        except RateLimitError as e:
            if not wait_on_rate_limit:
                raise
            async for pause_event in _wait_for_quota(e, agent=agent, phase=phase, round=round):
                yield pause_event
            continue  # re-run the identical call from scratch
        return


async def _run_once(
    *,
    prompt_path: Path,
    stdin_text: str,
    instruction: str,
    mode: Literal["text_only", "builder", "read_only", "builder_exec"],
    agent: Optional[str],
    phase: Optional[str],
    round: Optional[int],
    cwd: Optional[str],
    timeout: int,
    include_budget_flag: bool,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    max_turns: Optional[int] = None,
    env: Optional[dict[str, str]] = None,
) -> AsyncIterator[AgentEvent]:
    args = _build_args(
        prompt_path=prompt_path,
        instruction=instruction,
        mode=mode,
        include_budget_flag=include_budget_flag,
        model=model,
        effort=effort,
        max_turns=max_turns,
    )
    proc = await _spawn(args, cwd=cwd, env=env)

    async def _feed_stdin(p: asyncio.subprocess.Process) -> None:
        try:
            p.stdin.write(stdin_text.encode("utf-8", errors="replace"))
            await p.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            # CLI exited (e.g. rejected a flag) before consuming stdin -- harmless.
            pass
        finally:
            try:
                p.stdin.close()
            except Exception:
                pass

    async def _drain_stderr(p: asyncio.subprocess.Process) -> bytes:
        chunks = []
        async for line in p.stderr:
            chunks.append(line)
        return b"".join(chunks)

    stdin_task = asyncio.create_task(_feed_stdin(proc))
    stderr_task: Optional[asyncio.Task] = None
    try:
        async with asyncio.timeout(timeout):
            stderr_task = asyncio.create_task(_drain_stderr(proc))

            saw_partial = False
            full_text_parts: list[str] = []
            result_text: Optional[str] = None
            cost_usd: Optional[float] = None
            is_error = False
            result_subtype: Optional[str] = None
            api_error_status: Optional[int] = None
            result_errors: list[str] = []
            rate_limit_payload: Optional[dict] = None

            async for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    # Malformed stream-json line: log + skip, never crash the stream.
                    print(f"[runner] skipping malformed stream-json line: {line[:200]!r}", file=sys.stderr)
                    continue

                obj_type = obj.get("type")

                if obj_type == "stream_event":
                    event = obj.get("event") or {}
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                saw_partial = True
                                full_text_parts.append(text)
                                yield AgentEvent(
                                    type="delta", phase=phase, round=round, agent=agent, content=text
                                )

                elif obj_type == "assistant":
                    message = obj.get("message") or {}
                    for block in message.get("content") or []:
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            if text and not saw_partial:
                                # Fallback: this CLI build didn't honor --include-partial-messages.
                                full_text_parts.append(text)
                                yield AgentEvent(
                                    type="delta", phase=phase, round=round, agent=agent, content=text
                                )

                elif obj_type == "result":
                    result_text = obj.get("result")
                    cost_usd = obj.get("total_cost_usd")
                    is_error = bool(obj.get("is_error"))
                    result_subtype = obj.get("subtype")
                    # Both confirmed against the real CLI (audit M5): a budget abort has
                    # no "result"/stderr text -- its reason lives only in this "errors"
                    # array; an invalid model reports subtype "success" (!) and reveals
                    # the failure only via is_error + api_error_status.
                    api_error_status = obj.get("api_error_status")
                    result_errors = [str(e) for e in obj.get("errors") or []]

                elif obj_type == "rate_limit_event":
                    # Previously discarded entirely; captured now so a terminal failure
                    # can be checked against it in _detect_rate_limit below (Usage-Limit
                    # Resilience addon) -- the exact shape of this message isn't
                    # documented, so this is passed through as-is rather than parsed here.
                    rate_limit_payload = obj

                # system / anything else: ignore gracefully.

            await proc.wait()
            stderr_bytes = await stderr_task
    except asyncio.TimeoutError:
        await _kill_process_tree(proc)
        stdin_task.cancel()
        # stderr_task is a separate asyncio Task (not something this coroutine awaits
        # in between), so asyncio.timeout() cancelling *this* coroutine does not also
        # cancel it -- left uncancelled, it would keep running in the background
        # unretrieved, and any exception it eventually raised would vanish silently
        # ("Task exception was never retrieved") instead of surfacing anywhere.
        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass
        raise AgentError(f"agent call timed out after {timeout}s")
    except asyncio.CancelledError:
        # A user-initiated cancel (main.py's POST /run/{run_id}/cancel -> state.task.
        # cancel(), Usage-Limit Resilience addon) can land here mid-call, e.g. cancelling
        # a run that isn't currently paused. Without this, asyncio.CancelledError alone
        # would leave the claude.exe subprocess orphaned -- it doesn't touch the child
        # process by itself. Mirrors the timeout branch's cleanup, then re-raises so
        # cancellation propagates as a real cancellation rather than an AgentError:
        # callers must not retry a deliberate cancel the way they retry a genuine failure.
        await _kill_process_tree(proc)
        stdin_task.cancel()
        if stderr_task is not None:
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
        raise
    except Exception as e:
        # Any other failure while reading the stream -- most concretely a ValueError
        # ("Separator is found, but chunk is longer than limit") when a single stream-json
        # line exceeds config.STREAM_READ_LIMIT_BYTES (e.g. a builder-mode tool result
        # embedding a very large file). Without this branch the exception would propagate
        # with the claude subprocess still alive and its stdout pipe full/blocked -- an
        # orphaned process -- and stderr_task left unretrieved. Mirror the timeout/cancel
        # cleanup, then surface it as an AgentError so the caller's retry-once/error path
        # handles it uniformly instead of as a bare "internal error".
        #
        # Must stay below `except asyncio.TimeoutError` (TimeoutError IS an Exception
        # subclass and would otherwise be caught here and mislabeled). CancelledError is a
        # BaseException, not Exception, so it is never swallowed by this clause regardless.
        await _kill_process_tree(proc)
        stdin_task.cancel()
        if stderr_task is not None:
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
        raise AgentError(f"stream read failed: {e}") from e
    finally:
        # Never leave the feeder task unretrieved -- it only ever swallows its own errors.
        try:
            await stdin_task
        except asyncio.CancelledError:
            pass

    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    final_text = result_text if result_text is not None else "".join(full_text_parts)

    if proc.returncode != 0 or is_error:
        # A max-turns cutoff (subtype "error_max_turns") can land after the agent has
        # already streamed a complete-looking reply -- or, for builder-mode agents,
        # already written real files via tool calls -- before running out of turns.
        # The CLI's exit code / is_error flag alone can't tell that apart from a
        # genuine failure. If real output was already collected, treat this as a
        # truncated-but-usable result instead of raising: callers shouldn't discard
        # good output, or pay for a retry that mostly just redoes completed work.
        if result_subtype == "error_max_turns" and final_text:
            yield AgentEvent(
                type="result", phase=phase, round=round, agent=agent,
                content=final_text, cost_usd=cost_usd, truncated=True,
            )
            return
        # Usage-Limit Resilience addon: check before falling through to a plain
        # AgentError -- a subscription usage-limit exhaustion (or a transient 429) gets
        # a distinct exception type so run_agent_streaming can pause-and-resume instead
        # of the ordinary retry-once-then-fail path.
        rate_limit_error = _detect_rate_limit(
            result_text=result_text, stderr_text=stderr_text, rate_limit_payload=rate_limit_payload
        )
        if rate_limit_error is not None:
            raise rate_limit_error
        # stderr is often empty even on failure (e.g. a budget/turn-limit abort reports
        # its reason via the "result" NDJSON event, not stderr) -- prefer whichever
        # actually has content instead of always defaulting to a bare exit code.
        tail = (stderr_text or result_text or "; ".join(result_errors) or "").strip()[-STDERR_TAIL_CHARS:]
        # Deterministic failures skip the caller's retry-once (audit M5). Both
        # signatures confirmed empirically against the installed CLI:
        #   - subtype "error_max_budget_usd": --max-budget-usd exhausted mid-call
        #     (terminal_reason "budget_exhausted"); a retry re-spends the whole budget
        #     to hit the identical abort.
        #   - api_error_status 404: nonexistent/inaccessible --model. NOT detectable
        #     via subtype -- that arrives as "success" with is_error=true.
        deterministic = result_subtype == "error_max_budget_usd" or api_error_status == 404
        raise AgentError(
            tail or f"claude exited with code {proc.returncode}",
            retryable=not deterministic,
        )

    yield AgentEvent(
        type="result", phase=phase, round=round, agent=agent, content=final_text, cost_usd=cost_usd
    )


async def run_agent(**kwargs) -> str:
    """Convenience wrapper: consumes the stream, returns the full text."""
    full_text = ""
    async for event in run_agent_streaming(**kwargs):
        if event.type == "result":
            full_text = event.content
    return full_text


async def _smoke_test(instruction: str) -> None:
    print(f"[runner smoke test] instruction: {instruction!r}\n", flush=True)
    try:
        async for event in run_agent_streaming(
            system_prompt_file="debate/strategist.txt",
            stdin_text="PROJECT IDEA: (smoke test -- ignore persona constraints, just answer the instruction)\n",
            instruction=instruction,
            mode="text_only",
            agent="Strategist",
        ):
            if event.type == "delta":
                print(event.content, end="", flush=True)
            elif event.type == "result":
                print(f"\n\n--- done ---\ncost: ${event.cost_usd if event.cost_usd is not None else 0:.4f}")
    except AgentError as e:
        print(f"\n[AgentError] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    # Model output is arbitrary Unicode; a Windows console's legacy codepage (e.g.
    # cp1252) can't encode all of it and print() would crash. See agents/debate.py.
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    instruction = " ".join(sys.argv[1:]) or "write a haiku about pipelines"
    asyncio.run(_smoke_test(instruction))
