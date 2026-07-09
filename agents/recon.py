"""Codebase Analysis Mode (SPEC.md addon), Stage 8: the Recon agent.

Investigates an existing codebase -- already copied into a sandbox by Stage 7's
agents/sandbox.py -- to produce a grounded digest that Stage 9 will fold into the
debate loop's `idea` text. Headless and bus-first, same shape as agents/sandbox.py
and agents/build.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Optional

import config
from agents.events import AgentEvent, EventBus
from agents.runner import AgentError, run_agent_streaming


async def _run_recon_turn(
    bus: EventBus,
    *,
    sandbox_dir: str,
    description: str,
    cost_state: list[float],
) -> tuple[Optional[str], float, bool]:
    """Run the Recon agent once, streaming deltas onto the bus. Retries once on
    AgentError; on a second failure, emits an `error` event and returns
    (None, 0.0, False) so the caller can fall back to the raw description alone --
    mirrors debate.py's _run_turn / build.py's _run_step retry-once pattern.

    `cost_state` is a single-element list shared across the whole run (recon is always
    the first phase in codebase mode -- see main.py's _run_pipeline), mutated in place
    so this call's cost is folded into the running total before agent_done/error is
    emitted -- see debate.py's _run_turn for the identical rationale."""
    prompt_file, mode, timeout = config.RECON_AGENT
    for attempt in (1, 2):
        bus.emit(AgentEvent(type="agent_start", phase="recon", agent="Recon"))
        try:
            full_text: Optional[str] = None
            cost_usd = 0.0
            truncated = False
            async for event in run_agent_streaming(
                system_prompt_file=prompt_file,
                stdin_text=description,
                instruction=(
                    "Investigate the codebase in your working directory for the request on "
                    "stdin, then produce the codebase context digest now, following your "
                    "role and output-format rules exactly."
                ),
                mode=mode,
                agent="Recon",
                phase="recon",
                cwd=sandbox_dir,
                timeout=timeout,
            ):
                if event.type == "delta":
                    bus.emit(event)
                elif event.type in ("paused", "resumed"):
                    # Usage-Limit Resilience addon: run_agent_streaming is waiting out a
                    # usage-limit exhaustion internally and will retry the same call once
                    # it's over -- forward these straight through so the UI can show a
                    # paused state instead of Recon looking silently stuck.
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
                    phase="recon",
                    agent="Recon",
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
                AgentEvent(type="error", phase="recon", agent="Recon", content=tail, cost_usd=cost_state[0])
            )
            return None, 0.0, False

    return None, 0.0, False  # unreachable, keeps type checkers happy


async def run_recon(
    *,
    sandbox_dir: str,
    description: str,
    bus: EventBus,
    output_dir: Optional[str] = None,
    cost_state: Optional[list[float]] = None,
) -> dict:
    """Run the Recon agent against an already-prepared sandbox (agents/sandbox.py).

    Always returns a dict with a usable `context_text` -- either Recon's real digest,
    or a graceful fallback note if Recon failed twice -- so callers (Stage 9's debate
    loop) never have to special-case a missing Recon step. Writes codebase_context.md
    only when Recon actually produced text and an output_dir was given.

    `cost_state` lets a caller (main.py's _run_pipeline) share one running-total
    accumulator across multiple phases (recon -> debate -> build in codebase mode) so
    the frontend's cumulative cost display doesn't reset between phases. Defaults to a
    fresh [0.0] for headless/standalone callers that only ever run this one phase.
    """
    if cost_state is None:
        cost_state = [0.0]
    text, _cost, truncated = await _run_recon_turn(
        bus, sandbox_dir=sandbox_dir, description=description, cost_state=cost_state
    )

    result = {
        "sandbox_dir": sandbox_dir,
        "context_path": None,
        "context_text": "",
        "total_cost_usd": cost_state[0],
        "ok": text is not None,
        "truncated": truncated,
    }

    if text is not None:
        result["context_text"] = text
        if output_dir:
            context_path = Path(output_dir) / "codebase_context.md"
            context_path.write_text(text, encoding="utf-8")
            result["context_path"] = str(context_path)
    else:
        result["context_text"] = (
            "Recon was unavailable this run; no codebase context was gathered. "
            "Debate from the description alone."
        )

    bus.emit(
        AgentEvent(type="phase_done", phase="recon", content=json.dumps(result), cost_usd=cost_state[0])
    )
    return result


async def _headless_main(sandbox_dir: str, description: str) -> None:
    run_id = uuid.uuid4().hex[:8]
    out_dir = Path(config.OUTPUT_DIR) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    bus = EventBus()

    async def _consume() -> None:
        async for ev in bus.stream():
            if ev.type == "agent_start":
                print("\n=== Recon ===", flush=True)
            elif ev.type == "delta":
                print(ev.content, end="", flush=True)
            elif ev.type == "agent_done":
                print(flush=True)
            elif ev.type == "error":
                print(f"\n[ERROR] {ev.content}", file=sys.stderr, flush=True)
            elif ev.type == "phase_done":
                print(f"\n--- phase done: {ev.content}", flush=True)

    consumer_task = asyncio.create_task(_consume())
    result = await run_recon(
        sandbox_dir=sandbox_dir, description=description, bus=bus, output_dir=str(out_dir)
    )
    bus.close()
    await consumer_task

    print(f"\nrun_id: {run_id}")
    print(f"context_path: {result['context_path']}")
    print(f"ok: {result['ok']}")
    print(f"total_cost_usd: ${result['total_cost_usd']:.4f}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stage 8: Recon agent only, headless (Codebase Analysis Mode)."
    )
    parser.add_argument("sandbox_dir", help="path to an already-prepared sandbox (see agents/sandbox.py)")
    parser.add_argument("description", help="the bug/feature description to investigate")
    return parser.parse_args(argv)


if __name__ == "__main__":
    # Model output is arbitrary Unicode; a Windows console's legacy codepage (e.g.
    # cp1252) can't encode all of it and print() would crash. See agents/debate.py.
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = _parse_args(sys.argv[1:])
    asyncio.run(_headless_main(args.sandbox_dir, args.description))
