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
from typing import Optional

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
    timeout: Optional[int] = None,
) -> tuple[Optional[str], float]:
    """Run one agent turn, streaming deltas onto the bus. Retries once on AgentError;
    on a second failure, emits an `error` event and returns (None, 0.0) so the caller
    can add a system note and keep the debate going."""
    for attempt in (1, 2):
        bus.emit(AgentEvent(type="agent_start", phase=phase, round=round, agent=agent))
        try:
            full_text: Optional[str] = None
            cost_usd = 0.0
            async for event in run_agent_streaming(
                system_prompt_file=system_prompt_file,
                stdin_text=stdin_text,
                instruction=instruction,
                mode="text_only",
                agent=agent,
                phase=phase,
                round=round,
                timeout=timeout,
            ):
                if event.type == "delta":
                    bus.emit(event)
                elif event.type == "result":
                    full_text = event.content
                    cost_usd = event.cost_usd or 0.0
            bus.emit(AgentEvent(type="agent_done", phase=phase, round=round, agent=agent))
            return full_text, cost_usd
        except AgentError as e:
            if attempt == 1:
                continue
            tail = str(e).strip()[-500:]
            bus.emit(AgentEvent(type="error", phase=phase, round=round, agent=agent, content=tail))
            return None, 0.0

    return None, 0.0  # unreachable, keeps type checkers happy


async def run_debate(
    *, idea: str, num_rounds: int, bus: EventBus, run_id: str, output_dir: Optional[str] = None
) -> dict:
    """Run the full debate loop + final synthesis. Returns a summary dict; always writes
    debate_log.json, and writes agreed_spec.md only if the final synthesis succeeded."""
    num_rounds = max(1, min(num_rounds, config.MAX_DEBATE_ROUNDS))
    out_dir = Path(output_dir) if output_dir else Path(config.OUTPUT_DIR) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    history: list[dict] = []
    summary = ""
    total_cost = 0.0

    for r in range(1, num_rounds + 1):
        for name, prompt_file in config.DEBATE_AGENTS:
            stdin_text = render_transcript(idea, history, current_round=r, summary=summary)
            instruction = (
                f"You are speaking in round {r} of {num_rounds}. Read the transcript on stdin "
                "and reply now, in character, following your role and output-format rules."
            )
            text, cost = await _run_turn(
                bus,
                agent=name,
                system_prompt_file=prompt_file,
                stdin_text=stdin_text,
                instruction=instruction,
                phase="debate",
                round=r,
                timeout=config.DEBATE_TIMEOUT,
            )
            total_cost += cost
            if text is not None:
                history.append({"round": r, "agent": name, "text": text})
                if name == "Refiner":
                    summary = _update_summary(summary, text)
            else:
                history.append({"round": r, "agent": "system", "text": f"{name} was unavailable this round."})

    final_instruction_path = Path(config.PROMPTS_DIR) / config.REFINER_FINAL_INSTRUCTION
    final_instruction = final_instruction_path.read_text(encoding="utf-8")
    final_stdin = render_transcript(idea, history, current_round=1, summary=summary)  # force full

    final_text, final_cost = await _run_turn(
        bus,
        agent="Refiner",
        system_prompt_file="debate/refiner.txt",
        stdin_text=final_stdin,
        instruction=final_instruction,
        phase="debate",
        round=None,
        timeout=config.DEBATE_TIMEOUT,
    )
    total_cost += final_cost

    agreed_spec_path: Optional[Path] = None
    if final_text is not None:
        agreed_spec_path = out_dir / "agreed_spec.md"
        agreed_spec_path.write_text(final_text, encoding="utf-8")

    debate_log = {
        "run_id": run_id,
        "idea": idea,
        "num_rounds": num_rounds,
        "history": history,
        "total_cost_usd": total_cost,
    }
    (out_dir / "debate_log.json").write_text(json.dumps(debate_log, indent=2), encoding="utf-8")

    bus.emit(
        AgentEvent(
            type="phase_done",
            phase="debate",
            content=json.dumps(
                {
                    "agreed_spec_path": str(agreed_spec_path) if agreed_spec_path else None,
                    "total_cost_usd": total_cost,
                }
            ),
        )
    )

    return {
        "run_id": run_id,
        "output_dir": str(out_dir),
        "agreed_spec_path": str(agreed_spec_path) if agreed_spec_path else None,
        "history": history,
        "total_cost_usd": total_cost,
    }


async def _headless_main(idea: str, num_rounds: int) -> None:
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
        result = await run_debate(idea=idea, num_rounds=num_rounds, bus=bus, run_id=run_id)
    finally:
        bus.close()
    await consumer_task

    print(f"\nrun_id: {result['run_id']}")
    print(f"agreed_spec: {result['agreed_spec_path']}")
    print(f"total_cost_usd: ${result['total_cost_usd']:.4f}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Phase 1 debate headlessly.")
    parser.add_argument("idea", help="the project idea to debate")
    parser.add_argument("--rounds", type=int, default=config.DEFAULT_DEBATE_ROUNDS)
    return parser.parse_args(argv)


if __name__ == "__main__":
    # Model output is arbitrary Unicode (arrows, em-dashes, ...); a Windows console's
    # legacy codepage (e.g. cp1252) can't encode all of it and print() would crash.
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = _parse_args(sys.argv[1:])
    asyncio.run(_headless_main(args.idea, args.rounds))
