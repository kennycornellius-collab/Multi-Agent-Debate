"""FastAPI backend: run registry, SSE streaming, and output file endpoints.

Stage 4. Wires agents/debate.py -> agents/build.py through one shared EventBus
per run, driven by a background asyncio task started from POST /run. No agent
logic lives here -- this module only owns HTTP/SSE plumbing and the in-memory
run registry.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from agents.build import run_build
from agents.debate import run_debate
from agents.events import AgentEvent, EventBus
from agents.runner import AgentError, run_agent
from check_cli import run_preflight

logger = logging.getLogger(__name__)

OUTPUT_ROOT = Path(config.OUTPUT_DIR).resolve()
STATIC_DIR = Path("static")

# Set once at startup by the lifespan hook below; gates POST /run per SPEC.md's
# error-handling rule ("CLI missing/unauthenticated ... /run returns 503").
preflight_ok: bool = False
preflight_message: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    global preflight_ok, preflight_message
    preflight_ok, preflight_message = run_preflight()
    yield


app = FastAPI(title="Multi-Agent Debate & Build Pipeline", lifespan=lifespan)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@dataclass
class RunState:
    run_id: str
    bus: EventBus
    idea: str
    num_rounds: int
    phase: Optional[str] = None
    round: Optional[int] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    running: bool = True
    # Every error that happened during the run, in order -- not just the most recent
    # one. A run can have several agents fail permanently at different points (e.g.
    # Critic in round 1, Strategist in round 2); overwriting a single `error` field
    # would silently drop all but the last one from /status and the reload banner.
    errors: list[str] = field(default_factory=list)
    task: Optional[asyncio.Task] = None


# Single-user local tool -- no DB. Runs live only as long as the process does.
runs: dict[str, RunState] = {}


class RunRequest(BaseModel):
    idea: str
    num_rounds: int = config.DEFAULT_DEBATE_ROUNDS
    # Accepted for schema compatibility with SPEC.md's documented request body.
    # The build pipeline (agents/build.py) always runs its fixed four steps --
    # there is no per-run agent subset to wire this into yet.
    agents: list[str] = []
    # Optional per-run overrides for the whole pipeline (debate + build agents alike),
    # passed straight to the `claude` CLI's --model/--effort flags. None/omitted means
    # "use config.py's DEBATE_MODEL/BUILD_MODEL default", unchanged from before these
    # existed. Not restricted to config.AVAILABLE_MODELS -- that list is only the
    # browser UI's curated dropdown; POST /models/check is how an arbitrary value gets
    # verified.
    model: Optional[str] = None
    effort: Optional[str] = None


class ModelCheckRequest(BaseModel):
    model: str


async def _watch_status(state: RunState) -> None:
    """Keep RunState.phase/round/errors in sync with the bus, for GET /status."""
    async for ev in state.bus.stream():
        if ev.type == "agent_start":
            state.phase = ev.phase
            state.round = ev.round
        elif ev.type == "error":
            label = ev.agent or "system"
            if ev.round is not None:
                label += f" (round {ev.round})"
            state.errors.append(f"{label}: {ev.content}")
        elif ev.type == "run_done":
            state.running = False


async def _run_pipeline(state: RunState) -> None:
    watcher = asyncio.create_task(_watch_status(state))
    # Separate from state.errors: state.errors is also appended to by _watch_status
    # (asynchronously, from per-agent `error` events) and nothing here awaits in
    # between an agent's error event and this function's finally block, so reading
    # state.errors for run_done's content would race the watcher and could embed a
    # stale/incomplete value. fatal_error is only ever set synchronously, right here.
    fatal_error: Optional[str] = None
    try:
        debate_result = await run_debate(
            idea=state.idea,
            num_rounds=state.num_rounds,
            bus=state.bus,
            run_id=state.run_id,
            model=state.model,
            effort=state.effort,
        )
        if debate_result.get("agreed_spec_path"):
            await run_build(run_id=state.run_id, bus=state.bus, model=state.model, effort=state.effort)
        else:
            fatal_error = "Debate phase ended without an agreed spec; build phase skipped."
    except AgentError as e:
        fatal_error = str(e)
    except Exception as e:  # a bug here must not leave the run stuck as "running" forever
        # The UI only ever shows the stringified exception -- log the full traceback
        # server-side too, or a genuine bug is nearly impossible to diagnose from the
        # browser alone (see the Stage 5 --reload gotcha in progress.md).
        logger.exception("Unhandled error in _run_pipeline (run_id=%s)", state.run_id)
        fatal_error = f"internal error: {e}"
    finally:
        state.running = False
        if fatal_error:
            state.errors.append(fatal_error)
        state.bus.emit(AgentEvent(type="run_done", content=fatal_error or ""))
        state.bus.close()
        await watcher


def _error_banner_html(message: str) -> str:
    return (
        "<!doctype html><html><body style=\"font-family:sans-serif;max-width:640px;"
        "margin:4rem auto;line-height:1.5\">"
        "<h1 style=\"color:#b00020\">Claude Code CLI not ready</h1>"
        f"<pre style=\"white-space:pre-wrap\">{html.escape(message)}</pre>"
        "</body></html>"
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    if not preflight_ok:
        return _error_banner_html(preflight_message)
    index_path = STATIC_DIR / "index.html"
    if index_path.is_file():
        return index_path.read_text(encoding="utf-8")
    return (
        "<!doctype html><html><body style=\"font-family:sans-serif;max-width:640px;"
        "margin:4rem auto\"><h1>Multi-Agent Debate & Build Pipeline</h1>"
        "<p>Backend is up. Static UI not implemented yet (Stage 5).</p></body></html>"
    )


@app.get("/config")
async def get_ui_config() -> dict:
    """Curated model/effort options for the browser UI's dropdown + slider, sourced
    straight from config.py so the frontend never carries its own duplicate list."""
    return {"models": config.AVAILABLE_MODELS, "effort_levels": config.AVAILABLE_EFFORT_LEVELS}


@app.post("/run")
async def start_run(req: RunRequest) -> dict:
    if not preflight_ok:
        raise HTTPException(status_code=503, detail=preflight_message)
    if not req.idea.strip():
        raise HTTPException(status_code=400, detail="idea must not be empty")

    model = req.model.strip() if req.model and req.model.strip() else None
    effort = req.effort.strip() if req.effort and req.effort.strip() else None
    if effort and effort not in config.AVAILABLE_EFFORT_LEVELS:
        raise HTTPException(
            status_code=400,
            detail=f"invalid effort {effort!r}; must be one of {config.AVAILABLE_EFFORT_LEVELS}",
        )

    run_id = uuid.uuid4().hex[:8]
    bus = EventBus()
    state = RunState(
        run_id=run_id, bus=bus, idea=req.idea, num_rounds=req.num_rounds, model=model, effort=effort
    )
    runs[run_id] = state
    state.task = asyncio.create_task(_run_pipeline(state))
    return {"run_id": run_id}


@app.post("/models/check")
async def check_model(req: ModelCheckRequest) -> dict:
    """Cheap real availability probe for the browser UI's model dropdown/custom input.

    An invalid --model fails fast and free (confirmed against the real CLI: ~1s,
    total_cost_usd=0, is_error=True with a clear message) -- so this just makes that
    same real call and reports what actually happened, instead of guessing from a
    hardcoded list. A valid model incurs one small real call.
    """
    if not preflight_ok:
        raise HTTPException(status_code=503, detail=preflight_message)
    model = req.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="model must not be empty")
    try:
        await run_agent(
            system_prompt_file="debate/strategist.txt",
            stdin_text="(availability check -- ignore persona constraints, just answer the instruction)\n",
            instruction="Reply with the single word: ok",
            mode="text_only",
            model=model,
            timeout=60,
        )
        return {"available": True, "message": ""}
    except AgentError as e:
        return {"available": False, "message": str(e)}


@app.get("/stream/{run_id}")
async def stream(run_id: str) -> StreamingResponse:
    state = runs.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown run_id")

    async def event_gen():
        async for ev in state.bus.stream():
            yield f"data: {json.dumps(ev.to_dict())}\n\n"

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.get("/status/{run_id}")
async def status(run_id: str) -> dict:
    state = runs.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown run_id")
    return {
        "phase": state.phase,
        "round": state.round,
        "running": state.running,
        # Joined so the response schema stays a single string (per SPEC.md), but now
        # reflects every failure that occurred during the run, not just the last one.
        "error": "; ".join(state.errors) if state.errors else None,
    }


@app.get("/output/{run_id}")
async def list_output(run_id: str) -> dict:
    if run_id not in runs:
        raise HTTPException(status_code=404, detail="unknown run_id")
    run_dir = (OUTPUT_ROOT / run_id).resolve()
    if not run_dir.is_dir():
        return {"run_id": run_id, "files": []}
    files = sorted(
        str(p.relative_to(run_dir)).replace("\\", "/") for p in run_dir.rglob("*") if p.is_file()
    )
    return {"run_id": run_id, "files": files}


@app.get("/output/{run_id}/{path:path}")
async def get_output_file(run_id: str, path: str) -> PlainTextResponse:
    if run_id not in runs:
        raise HTTPException(status_code=404, detail="unknown run_id")
    run_dir = (OUTPUT_ROOT / run_id).resolve()
    requested = (run_dir / path).resolve()
    if not requested.is_relative_to(run_dir):
        raise HTTPException(status_code=403, detail="path escapes the run's output directory")
    if not requested.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return PlainTextResponse(requested.read_text(encoding="utf-8", errors="replace"))
