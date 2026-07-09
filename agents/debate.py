"""Phase 1: the debate loop (Strategist -> Critic -> Refiner x N rounds) plus the
final Refiner synthesis call that produces agreed_spec.md.

Headless and bus-first: `run_debate()` takes an EventBus + run_id so Stage 4's FastAPI
backend can drive the exact same function a browser-triggered run uses. The __main__
entrypoint below is just a terminal consumer wired onto that same bus.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Literal, Optional

import config
from agents.events import AgentEvent, EventBus
from agents.runner import AgentError, run_agent_streaming

# Half of the ~200-word running-summary budget (see CLAUDE.md's history policy) --
# split between the prior summary's tail and the latest reply's head so one side can
# never fully evict the other. See _update_summary's docstring for why.
SUMMARY_HALF_WORDS = 100


def render_transcript(idea: str, history: list[dict], *, current_round: int, summary: str) -> str:
    """Plain-text transcript rendering (not JSON -- models follow it better).

    Rounds 1-2 (current_round <= HISTORY_FULL_ROUNDS): full history.
    Round 3+: a running summary + the last HISTORY_TAIL_MESSAGES verbatim.

    Passing current_round=1 always takes the full-history branch regardless of how many
    rounds actually happened -- used deliberately for the final synthesis call, which must
    see everything.
    """
    lines = [f"PROJECT IDEA: {idea}", ""]
    if not history:
        return "\n".join(lines) + "\n"

    if current_round <= config.HISTORY_FULL_ROUNDS:
        messages = history
    else:
        messages = []
        if summary:
            lines.append(f"SUMMARY: {summary}")
            lines.append("")
        messages = history[-config.HISTORY_TAIL_MESSAGES :]

    for msg in messages:
        lines.append(f"[Round {msg['round']}] {msg['agent']}: {msg['text']}")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def _update_summary(prev_summary: str, latest_refiner_text: str) -> str:
    """Blend the Refiner's latest reply into the running summary, capped at ~200 words
    total. No extra CLI call needed -- the Refiner's own synthesis IS the summary source.

    Each side gets a fixed half of the budget (SUMMARY_HALF_WORDS each) rather than
    prepending the latest reply and keeping the first 200 words combined -- refiner.txt
    allows replies up to ~250 words, so a single reply could previously evict the entire
    prior summary on its own (verified: after 3 rounds of ~200-word replies, 0 words
    survived from round 1 and only 10/200 from round 2). Keeping the *tail* of the prior
    summary (its most recently-added round) alongside the *head* of the latest reply
    guarantees every round's ruling survives into at least the following round's prompt,
    matching the Refiner's own convergence rule (don't relitigate the immediately
    preceding round)."""
    prev_words = prev_summary.split()[-SUMMARY_HALF_WORDS:]
    latest_words = latest_refiner_text.split()[:SUMMARY_HALF_WORDS]
    return " ".join(prev_words + latest_words)


async def _run_turn(
    bus: EventBus,
    *,
    agent: str,
    system_prompt_file: str,
    stdin_text: str,
    instruction: str,
    phase: str,
    round: Optional[int],
    cost_state: list[float],
    mode: Literal["text_only", "read_only"] = "text_only",
    cwd: Optional[str] = None,
    timeout: Optional[int] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
) -> tuple[Optional[str], float, bool]:
    """Run one agent turn, streaming deltas onto the bus. Retries once on AgentError;
    on a second failure, emits an `error` event and returns (None, 0.0, False) so the
    caller can add a system note and keep the debate going.

    `mode`/`cwd` default to a pure text_only turn with no cwd (every debate agent,
    always, before Codebase Analysis Mode existed). Codebase mode's Critic turn is the
    only caller that passes mode="read_only"/cwd=<sandbox> -- see run_debate().

    The third element of the return tuple is `truncated`: True if the CLI hit its
    turn limit but had already produced usable output (see agents/runner.py) -- mirrors
    build.py's _run_step, so a truncated debate turn is surfaced the same way a
    truncated build step is, instead of being silently treated as a clean finish.

    `cost_state` is a single-element list shared across the whole run (potentially
    across recon/debate/build phases -- see main.py's _run_pipeline), mutated in place
    so this turn's cost is folded into the running total *before* agent_done/error is
    emitted -- that's what lets the frontend show a live cumulative cost per event
    instead of only learning the total at the very end of the run."""
    for attempt in (1, 2):
        bus.emit(AgentEvent(type="agent_start", phase=phase, round=round, agent=agent))
        try:
            full_text: Optional[str] = None
            cost_usd = 0.0
            truncated = False
            async for event in run_agent_streaming(
                system_prompt_file=system_prompt_file,
                stdin_text=stdin_text,
                instruction=instruction,
                mode=mode,
                cwd=cwd,
                agent=agent,
                phase=phase,
                round=round,
                timeout=timeout,
                model=model,
                effort=effort,
            ):
                if event.type == "delta":
                    bus.emit(event)
                elif event.type in ("paused", "resumed"):
                    # Usage-Limit Resilience addon: run_agent_streaming is waiting out a
                    # usage-limit exhaustion internally and will retry the same call once
                    # it's over -- forward these straight through so the UI can show a
                    # paused state instead of the turn looking silently stuck.
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
                    phase=phase,
                    round=round,
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
                AgentEvent(
                    type="error", phase=phase, round=round, agent=agent, content=tail, cost_usd=cost_state[0]
                )
            )
            return None, 0.0, False

    return None, 0.0, False  # unreachable, keeps type checkers happy


async def run_debate(
    *,
    idea: str,
    num_rounds: int,
    bus: EventBus,
    run_id: str,
    output_dir: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    sandbox_dir: Optional[str] = None,
    cost_state: Optional[list[float]] = None,
) -> dict:
    """Run the full debate loop + final synthesis. Returns a summary dict; always writes
    debate_log.json, and writes agreed_spec.md only if the final synthesis succeeded.

    `model`/`effort` are optional per-run overrides (e.g. from the browser UI) applied to
    every agent call in this run, debate and final synthesis alike; omitted, they fall
    back to config.py's DEBATE_MODEL default via agents/runner.py.

    `sandbox_dir` is Codebase Analysis Mode's hook (SPEC.md Stage 9): when set, it's an
    already-prepared sandbox (agents/sandbox.py) whose presence *is* codebase mode --
    no separate bool needed. The only thing it changes: Critic's turn runs
    mode="read_only" with cwd=sandbox_dir instead of text_only, so it can verify claims
    (especially Recon's) against the real files instead of only arguing from the
    transcript. Strategist and Refiner are unaffected. The codebase context itself is
    expected to already be folded into `idea` by the caller (e.g. Recon's digest) --
    run_debate() doesn't know or care where `idea` came from.

    `cost_state` lets a caller (main.py's _run_pipeline) share one running-total
    accumulator across multiple phases (e.g. recon -> debate -> build in codebase mode)
    so the frontend's cumulative cost display doesn't reset between phases. Defaults to
    a fresh [0.0] for headless/standalone callers that only ever run this one phase."""
    num_rounds = max(1, min(num_rounds, config.MAX_DEBATE_ROUNDS))
    out_dir = Path(output_dir) if output_dir else Path(config.OUTPUT_DIR) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    if cost_state is None:
        cost_state = [0.0]

    history: list[dict] = []
    summary = ""
    warnings: list[str] = []

    for r in range(1, num_rounds + 1):
        for name, prompt_file in config.DEBATE_AGENTS:
            stdin_text = render_transcript(idea, history, current_round=r, summary=summary)
            instruction = (
                f"You are speaking in round {r} of {num_rounds}. Read the transcript on stdin "
                "and reply now, in character, following your role and output-format rules."
            )
            agent_mode: Literal["text_only", "read_only"] = "text_only"
            agent_cwd: Optional[str] = None
            agent_timeout = config.DEBATE_TIMEOUT
            if sandbox_dir and name == "Critic":
                # Only Critic gets tool access in codebase mode -- giving all three
                # agents read access would triple the tool-call cost per round for a
                # benefit only Critic's skeptical role actually needs (SPEC.md).
                agent_mode = "read_only"
                agent_cwd = sandbox_dir
                agent_timeout = config.RECON_TIMEOUT  # real file exploration, not a plain reply
                instruction += (
                    "\n\nYou have read-only access to the actual codebase in your working "
                    "directory. Verify claims in the transcript -- especially Recon's -- "
                    "against the real files before critiquing. Cite file path + line for "
                    "anything you confirm or refute."
                )
            text, _cost, truncated = await _run_turn(
                bus,
                agent=name,
                system_prompt_file=prompt_file,
                stdin_text=stdin_text,
                instruction=instruction,
                phase="debate",
                round=r,
                cost_state=cost_state,
                mode=agent_mode,
                cwd=agent_cwd,
                timeout=agent_timeout,
                model=model,
                effort=effort,
            )
            if truncated:
                warnings.append(f"{name} (round {r}) hit the turn limit; using the output produced so far.")
            if text is not None:
                history.append({"round": r, "agent": name, "text": text})
                if name == "Refiner":
                    summary = _update_summary(summary, text)
            else:
                history.append({"round": r, "agent": "system", "text": f"{name} was unavailable this round."})

    final_instruction_path = Path(config.PROMPTS_DIR) / config.REFINER_FINAL_INSTRUCTION
    final_instruction = final_instruction_path.read_text(encoding="utf-8")
    if sandbox_dir:
        # One conditional line (SPEC.md Stage 9), appended in code rather than a second
        # near-duplicate prompt file -- same "force_full" precedent as the transcript
        # trick above: one template, one branch condition.
        final_instruction += (
            "\n\nNote: this is Codebase Analysis Mode -- the debate concerns changes to an "
            "existing codebase (see the codebase context folded into the project idea above). "
            "The File Plan section should describe edits to existing paths (and any genuinely "
            "new files), not a fresh project tree."
        )
    final_stdin = render_transcript(idea, history, current_round=1, summary=summary)  # force full

    final_text, _final_cost, final_truncated = await _run_turn(
        bus,
        agent="Refiner",
        system_prompt_file="debate/refiner.txt",
        stdin_text=final_stdin,
        instruction=final_instruction,
        phase="debate",
        round=None,
        cost_state=cost_state,
        timeout=config.DEBATE_TIMEOUT,
        model=model,
        effort=effort,
    )
    if final_truncated:
        warnings.append("Refiner (final synthesis) hit the turn limit; using the output produced so far.")

    agreed_spec_path: Optional[Path] = None
    if final_text is not None:
        agreed_spec_path = out_dir / "agreed_spec.md"
        agreed_spec_path.write_text(final_text, encoding="utf-8")

    debate_log = {
        "run_id": run_id,
        "idea": idea,
        "num_rounds": num_rounds,
        "history": history,
        "total_cost_usd": cost_state[0],
        "warnings": warnings,
    }
    (out_dir / "debate_log.json").write_text(json.dumps(debate_log, indent=2), encoding="utf-8")

    bus.emit(
        AgentEvent(
            type="phase_done",
            phase="debate",
            content=json.dumps(
                {
                    "agreed_spec_path": str(agreed_spec_path) if agreed_spec_path else None,
                    "total_cost_usd": cost_state[0],
                    "warnings": warnings,
                }
            ),
            cost_usd=cost_state[0],
        )
    )

    return {
        "run_id": run_id,
        "output_dir": str(out_dir),
        "agreed_spec_path": str(agreed_spec_path) if agreed_spec_path else None,
        "history": history,
        "total_cost_usd": cost_state[0],
        "warnings": warnings,
    }


async def _headless_main(idea: str, num_rounds: int, sandbox_dir: Optional[str] = None) -> None:
    run_id = uuid.uuid4().hex[:8]
    bus = EventBus()

    async def _consume() -> None:
        async for ev in bus.stream():
            if ev.type == "agent_start":
                label = f"Round {ev.round}" if ev.round else "Final synthesis"
                print(f"\n=== [{label}] {ev.agent} ===", flush=True)
            elif ev.type == "delta":
                print(ev.content, end="", flush=True)
            elif ev.type == "agent_done":
                print(flush=True)
            elif ev.type == "error":
                print(f"\n[ERROR] {ev.agent}: {ev.content}", file=sys.stderr, flush=True)
            elif ev.type == "phase_done":
                print(f"\n--- phase done: {ev.content}", flush=True)

    consumer_task = asyncio.create_task(_consume())
    try:
        result = await run_debate(
            idea=idea, num_rounds=num_rounds, bus=bus, run_id=run_id, sandbox_dir=sandbox_dir
        )
    finally:
        bus.close()
    await consumer_task

    print(f"\nrun_id: {result['run_id']}")
    print(f"agreed_spec: {result['agreed_spec_path']}")
    print(f"total_cost_usd: ${result['total_cost_usd']:.4f}")
    if result.get("warnings"):
        print("warnings:")
        for w in result["warnings"]:
            print(f"  - {w}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Phase 1 debate headlessly.")
    parser.add_argument("idea", help="the project idea to debate")
    parser.add_argument("--rounds", type=int, default=config.DEFAULT_DEBATE_ROUNDS)
    parser.add_argument(
        "--sandbox",
        default=None,
        help="path to an already-prepared sandbox (agents/sandbox.py) -- enables Codebase "
        "Analysis Mode: Critic gets read-only file access instead of pure text_only",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    # Model output is arbitrary Unicode (arrows, em-dashes, ...); a Windows console's
    # legacy codepage (e.g. cp1252) can't encode all of it and print() would crash.
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = _parse_args(sys.argv[1:])
    asyncio.run(_headless_main(args.idea, args.rounds, sandbox_dir=args.sandbox))
