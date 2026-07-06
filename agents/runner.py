"""The ONLY module in this codebase that shells out to the `claude` CLI.

Every other module calls `run_agent_streaming()` or `run_agent()` from here.
No `anthropic` SDK import, no `api.anthropic.com`, no `ANTHROPIC_API_KEY` --
authentication is entirely the user's existing `claude` CLI login.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import AsyncIterator, Literal, Optional

import config
from agents.events import AgentEvent

CLAUDE_BIN = shutil.which("claude")

STDERR_TAIL_CHARS = 500


class AgentError(Exception):
    """Raised when an agent invocation fails (nonzero exit, timeout, bad setup)."""


def _resolve_prompt_file(system_prompt_file: str) -> Path:
    path = Path(config.PROMPTS_DIR) / system_prompt_file
    if not path.is_file():
        raise AgentError(f"system prompt file not found: {path}")
    return path.resolve()


def _build_args(
    *,
    prompt_path: Path,
    instruction: str,
    mode: Literal["text_only", "builder"],
    include_budget_flag: bool,
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
        model = config.DEBATE_MODEL
    elif mode == "builder":
        args += [
            "--permission-mode",
            "acceptEdits",
            "--max-turns",
            str(config.BUILD_MAX_TURNS),
        ]
        model = config.BUILD_MODEL
    else:
        raise AgentError(f"unknown mode: {mode!r}")

    if model:
        args += ["--model", model]

    if include_budget_flag:
        args += ["--max-budget-usd", str(config.MAX_BUDGET_USD_PER_CALL)]

    return args


def _looks_like_unknown_flag_error(stderr: str) -> bool:
    lowered = stderr.lower()
    return "--max-budget-usd" in stderr or "unknown option" in lowered or "unrecognized" in lowered


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


async def _spawn(args: list[str], *, cwd: Optional[str]) -> asyncio.subprocess.Process:
    if CLAUDE_BIN is None:
        raise AgentError("`claude` CLI not found on PATH")
    return await asyncio.create_subprocess_exec(
        CLAUDE_BIN,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        limit=config.STREAM_READ_LIMIT_BYTES,
    )


async def run_agent_streaming(
    *,
    system_prompt_file: str,
    stdin_text: str,
    instruction: str,
    mode: Literal["text_only", "builder"],
    agent: Optional[str] = None,
    phase: Optional[str] = None,
    round: Optional[int] = None,
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
) -> AsyncIterator[AgentEvent]:
    """Spawn the `claude` CLI for one agent turn and yield AgentEvents as output streams in.

    Yields `delta` events as text arrives, then a final `result` event carrying the
    full text and reported cost. Raises AgentError on nonzero exit or timeout.

    Guarded --max-budget-usd: if the CLI rejects the flag as unrecognized (older/newer
    builds), retry once without it -- transparently, before any output has been yielded.
    """
    if mode == "builder" and not cwd:
        raise AgentError("mode='builder' requires cwd")

    prompt_path = _resolve_prompt_file(system_prompt_file)
    if timeout is None:
        timeout = config.DEBATE_TIMEOUT if mode == "text_only" else config.CODER_TIMEOUT

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
    )

    gen = _run_once(**common_kwargs, include_budget_flag=True)
    try:
        first_event = await gen.__anext__()
    except StopAsyncIteration:
        return
    except AgentError as e:
        if _looks_like_unknown_flag_error(str(e)):
            print("[runner] --max-budget-usd rejected by this CLI build; retrying without it", file=sys.stderr)
            gen = _run_once(**common_kwargs, include_budget_flag=False)
            first_event = await gen.__anext__()
        else:
            raise

    yield first_event
    async for event in gen:
        yield event


async def _run_once(
    *,
    prompt_path: Path,
    stdin_text: str,
    instruction: str,
    mode: Literal["text_only", "builder"],
    agent: Optional[str],
    phase: Optional[str],
    round: Optional[int],
    cwd: Optional[str],
    timeout: int,
    include_budget_flag: bool,
) -> AsyncIterator[AgentEvent]:
    args = _build_args(
        prompt_path=prompt_path,
        instruction=instruction,
        mode=mode,
        include_budget_flag=include_budget_flag,
    )
    proc = await _spawn(args, cwd=cwd)

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
    try:
        async with asyncio.timeout(timeout):
            stderr_task = asyncio.create_task(_drain_stderr(proc))

            saw_partial = False
            full_text_parts: list[str] = []
            result_text: Optional[str] = None
            cost_usd: Optional[float] = None
            is_error = False
            result_subtype: Optional[str] = None

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

                # system / rate_limit_event / anything else: ignore gracefully.

            await proc.wait()
            stderr_bytes = await stderr_task
    except asyncio.TimeoutError:
        await _kill_process_tree(proc)
        stdin_task.cancel()
        raise AgentError(f"agent call timed out after {timeout}s")
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
        # stderr is often empty even on failure (e.g. a budget/turn-limit abort reports
        # its reason via the "result" NDJSON event, not stderr) -- prefer whichever
        # actually has content instead of always defaulting to a bare exit code.
        tail = (stderr_text or result_text or "").strip()[-STDERR_TAIL_CHARS:]
        raise AgentError(tail or f"claude exited with code {proc.returncode}")

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
