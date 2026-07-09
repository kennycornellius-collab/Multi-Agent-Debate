"""Event dataclass + per-run event bus.

Every run owns one EventBus: an asyncio.Queue for live delivery plus a replay
list of every event emitted so far. SSE clients (Stage 4) flush the replay
list first, then stream live events off the queue.

Field naming note: the text payload is always carried in `content` (not
`text`) so it matches the SSE event schema in SPEC.md verbatim -- Stage 4's
`GET /stream/{run_id}` can `json.dumps(event.to_dict())` with zero
translation. The `result` event additionally carries `cost_usd`; so do
`agent_done`, `error`, and `phase_done` (a running total-so-far for the whole
pipeline run, not just that one call -- see debate.py/build.py/recon.py's
`cost_state` accumulator), which is what lets the frontend show a live
cumulative cost without any new event type or field.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import AsyncIterator, Optional

# Sentinel object placed on the queue to signal "no more events, stop iterating".
_DONE = object()


@dataclass
class AgentEvent:
    """One unit of pipeline activity.

    type: agent_start | delta | agent_done | phase_done | files_updated
          | error | run_done | result | paused | resumed
    phase: "debate" | "build" | None
    round: debate round number, or None outside debate
    agent: agent name (e.g. "Strategist") or "system"
    content: meaning depends on `type` (delta text, error message, file
             listing JSON, full result text, human-readable pause reason, ...)
    cost_usd: populated on `result` events when the CLI reports a cost, and on
              `agent_done`/`error`/`phase_done` as the pipeline's running total
              cost so far (see debate.py/build.py/recon.py)
    truncated: on a `result` event, True if the CLI hit its turn limit before
               finishing (subtype "error_max_turns") but had already produced
               usable output -- see agents/runner.py. False for a clean finish.
    retry_at: on a `paused` event (Usage-Limit Resilience addon), an ISO-8601
              timestamp string for when the call will be retried, or None if no
              reset time could be parsed (a fixed poll interval is used instead --
              see config.RATE_LIMIT_POLL_SECONDS). Lets the UI render a concrete
              "resumes ~3:45pm" without parsing `content`. Unused on every other
              event type.
    """

    type: str
    phase: Optional[str] = None
    round: Optional[int] = None
    agent: Optional[str] = None
    content: str = ""
    cost_usd: Optional[float] = None
    truncated: bool = False
    retry_at: Optional[str] = None
    seq: int = 0  # stamped by EventBus.emit; 0 until then

    def to_dict(self) -> dict:
        return asdict(self)


class EventBus:
    """Per-run event bus: replay buffer + seq counter + fan-out to live subscribers.

    Each `stream()` call gets its own queue rather than sharing one global queue.
    That is what makes replay-then-live correct for a subscriber that joins
    *after* some events already happened (Stage 4's SSE reconnect case): it takes
    a snapshot of the replay buffer, registers its own queue, then only ever sees
    events emitted from that point on -- no duplicates, no gaps.
    """

    def __init__(self) -> None:
        self.replay: list[AgentEvent] = []
        self._subscribers: list[asyncio.Queue] = []
        self._seq = 0
        self._closed = False

    def emit(self, event: AgentEvent) -> AgentEvent:
        """Stamp seq, append to replay, push to every live subscriber queue."""
        self._seq += 1
        event.seq = self._seq
        self.replay.append(event)
        for queue in self._subscribers:
            queue.put_nowait(event)
        return event

    def close(self) -> None:
        """Signal that no further events will be emitted (ends `stream()` iteration
        for every subscriber currently attached)."""
        if self._closed:
            return
        self._closed = True
        for queue in self._subscribers:
            queue.put_nowait(_DONE)

    async def stream(self) -> AsyncIterator[AgentEvent]:
        """Yield replay events first, then live events until `close()` is called.

        Safe to call any number of times, at any point in the run's lifetime,
        from any number of concurrent consumers -- each call is an independent
        subscription. Intended for Stage 4's SSE handler and the Stage 1 smoke test.
        """
        # Snapshot replay + note closed-ness before registering: nothing can run
        # between these lines (no `await`), so this is race-free under asyncio.
        replay_snapshot = list(self.replay)
        already_closed = self._closed
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            for event in replay_snapshot:
                yield event
            if already_closed:
                # Bus finished before we subscribed; no live events will ever come
                # and close() already fired (before we existed), so stop here.
                return
            while True:
                item = await queue.get()
                if item is _DONE:
                    return
                yield item
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)
