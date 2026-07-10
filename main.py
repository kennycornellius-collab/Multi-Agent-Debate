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
from typing import Literal, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import Headers

import config
from agents.build import run_build
from agents.debate import run_debate
from agents.events import AgentEvent, EventBus
from agents.recon import run_recon
from agents.runner import AgentError, run_agent
from agents.sandbox import prepare_sandbox
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


def _trusted_hostname(value: Optional[str]) -> bool:
    """True if `value` -- a Host header ("127.0.0.1:8000", "[::1]:8000") or an Origin URL
    ("http://localhost:8000") -- names a config.TRUSTED_HOSTS hostname. Port is ignored;
    anything unparseable (including the literal Origin "null" a sandboxed iframe sends,
    which urlsplit reads as hostname "null") is untrusted."""
    if not value:
        return False
    try:
        # A bare Host header has no scheme; "//" prefix makes urlsplit treat it as a
        # netloc (handling ports and [::1] brackets) instead of a path.
        hostname = urlsplit(value if "//" in value else f"//{value}").hostname
    except ValueError:
        return False
    return hostname in config.TRUSTED_HOSTS


class LocalOnlyMiddleware:
    """Reject requests that don't look like they come from this machine's own user (see
    config.TRUSTED_HOSTS for the threat model: CSRF + DNS rebinding against a local,
    unauthenticated server whose POSTs spend real quota and can execute code).

    Pure ASGI rather than Starlette's BaseHTTPMiddleware on purpose: BaseHTTPMiddleware
    re-wraps streaming responses, which can delay client-disconnect detection on the
    long-lived SSE endpoint -- this way /stream passes through untouched."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = Headers(scope=scope)
            if not _trusted_hostname(headers.get("host")):
                response = PlainTextResponse(
                    "rejected: Host header is not a local hostname (see config.TRUSTED_HOSTS)",
                    status_code=403,
                )
                await response(scope, receive, send)
                return
            origin = headers.get("origin")
            if origin is not None and not _trusted_hostname(origin):
                response = PlainTextResponse(
                    "rejected: cross-origin request (see config.TRUSTED_HOSTS)",
                    status_code=403,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


app.add_middleware(LocalOnlyMiddleware)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@dataclass
class RunState:
    run_id: str
    bus: EventBus
    idea: str
    num_rounds: int
    mode: Literal["full", "debate_only", "build_only", "codebase"] = "full"
    spec_text: str = ""  # build_only only: the user-supplied spec, written as agreed_spec.md
    target_path: str = ""  # codebase only: path to the existing codebase to patch
    phase: Optional[str] = None
    round: Optional[int] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    # Test Execution addon (SPEC.md v6): opt-in, default False. See RunRequest's field
    # for the full rationale (why this defaults off).
    allow_test_execution: bool = False
    running: bool = True
    # Every error that happened during the run, in order -- not just the most recent
    # one. A run can have several agents fail permanently at different points (e.g.
    # Critic in round 1, Strategist in round 2); overwriting a single `error` field
    # would silently drop all but the last one from /status and the reload banner.
    errors: list[str] = field(default_factory=list)
    # Usage-Limit Resilience addon: True while a call is waiting out a usage-limit
    # exhaustion (agents/runner.py's wait/resume loop, threaded through via `paused`/
    # `resumed` events). paused_until mirrors the event's retry_at (ISO string, or None
    # if no reset time was parseable -- see runner.py's _wait_for_quota).
    paused: bool = False
    paused_until: Optional[str] = None
    task: Optional[asyncio.Task] = None


# Single-user local tool -- no DB. Runs live only as long as the process does.
runs: dict[str, RunState] = {}


class RunRequest(BaseModel):
    # Required for mode="full"/"debate_only" (the debate loop's starting prompt);
    # ignored for mode="build_only", which skips the debate loop entirely. Also required
    # for mode="codebase", where it's reused with a new meaning: the bug/feature
    # description Recon investigates (SPEC.md's addon, same field, new meaning).
    idea: str = ""
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
    # "full" (default): debate then build, unchanged original behavior.
    # "debate_only": just the debate loop -- produces agreed_spec.md, no build agents run.
    # "build_only": skips the debate loop; spec_text (below) is written straight to
    # agreed_spec.md and the build pipeline runs against it -- lets a user who already
    # has a spec/schema skip straight to code generation.
    # "codebase": Codebase Analysis Mode (SPEC.md addon) -- sandboxes target_path (below),
    # runs Recon against it, folds Recon's digest into `idea`, runs the debate with
    # Critic's read-only sandbox access, then patches the sandbox in place instead of
    # scaffolding a fresh tree. Produces patch.diff; target_path itself is never written to.
    mode: Literal["full", "debate_only", "build_only", "codebase"] = "full"
    # Required for mode="build_only" (a user-authored spec, arbitrary markdown/text --
    # not validated against the 6-section template; the Architect handles it as-is,
    # same as any other agent input in this codebase). Ignored otherwise.
    spec_text: str = ""
    # Required for mode="codebase": path to the existing codebase to analyze/patch. Does
    # not need to be a git repo, need never have been committed, and needs no relation to
    # GitHub (SPEC.md). Only ever read once by the sandbox-prep step; never written to.
    # Ignored otherwise.
    target_path: str = ""
    # Test Execution addon (SPEC.md v6): opt-in, default False. When True, the Tester
    # actually invokes the test command it documents (builder_exec mode, a real Bash
    # grant scoped by a command-prefix denylist -- config.BUILD_EXEC_DISALLOWED_TOOLS,
    # not an allowlist -- see agents/runner.py). Defaults off because this is a genuine,
    # new category of risk -- especially for mode="codebase", where it means executing
    # code from the analyzed target_path, not just this pipeline's own output. When
    # False (every run before this addon, and every run that doesn't opt in), behavior
    # is byte-for-byte unchanged: Tester never gets a Bash tool.
    allow_test_execution: bool = False


class ModelCheckRequest(BaseModel):
    model: str


async def _watch_status(state: RunState) -> None:
    """Keep RunState.phase/round/errors/paused in sync with the bus, for GET /status."""
    async for ev in state.bus.stream():
        if ev.type == "agent_start":
            state.phase = ev.phase
            state.round = ev.round
        elif ev.type == "error":
            label = ev.agent or "system"
            if ev.round is not None:
                label += f" (round {ev.round})"
            state.errors.append(f"{label}: {ev.content}")
        elif ev.type == "paused":
            # Usage-Limit Resilience addon: a call is waiting out a usage-limit
            # exhaustion. retry_at may be None (no reset time parseable) -- the
            # heartbeat re-emits periodically either way, so paused_until just tracks
            # whatever the latest heartbeat said.
            state.paused = True
            state.paused_until = ev.retry_at
        elif ev.type == "resumed":
            state.paused = False
            state.paused_until = None
        elif ev.type == "run_done":
            state.running = False
            state.paused = False
            state.paused_until = None


async def _run_pipeline(state: RunState) -> None:
    watcher = asyncio.create_task(_watch_status(state))
    # Separate from state.errors: state.errors is also appended to by _watch_status
    # (asynchronously, from per-agent `error` events) and nothing here awaits in
    # between an agent's error event and this function's finally block, so reading
    # state.errors for run_done's content would race the watcher and could embed a
    # stale/incomplete value. fatal_error is only ever set synchronously, right here.
    fatal_error: Optional[str] = None
    # Shared across every phase this run touches (recon/debate/build) so the frontend's
    # cumulative cost display (agent_done/error/phase_done's cost_usd field) keeps
    # accruing across phase boundaries instead of resetting to 0 when build starts.
    cost_state: list[float] = [0.0]
    try:
        if state.mode == "build_only":
            # No debate call at all -- the user's own spec_text stands in for what the
            # debate loop would have produced, written to the same path run_build()
            # already expects to read (agents/build.py's own contract, unchanged).
            out_dir = Path(config.OUTPUT_DIR) / state.run_id
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "agreed_spec.md").write_text(state.spec_text, encoding="utf-8")
            await run_build(
                run_id=state.run_id,
                bus=state.bus,
                model=state.model,
                effort=state.effort,
                cost_state=cost_state,
                allow_exec=state.allow_test_execution,
            )
        elif state.mode == "codebase":
            # Codebase Analysis Mode (SPEC.md addon): chain Stage 7 (sandbox) -> Stage 8
            # (Recon) -> Stage 9 (debate with Critic's read-only sandbox access) -> Stage
            # 10 (patch-build). Every step shares this run's bus, same as full/debate_only/
            # build_only above -- one live feed regardless of mode.
            sandbox_result = await prepare_sandbox(
                target_path=state.target_path, run_id=state.run_id, bus=state.bus
            )
            if not sandbox_result["ok"]:
                fatal_error = (
                    f"Sandbox preparation failed ({sandbox_result.get('reason', 'unknown')}); "
                    "the run stopped before any agent call was made."
                )
            else:
                sandbox_dir = sandbox_result["build_dir"]
                diff_available = sandbox_result["diff_available"]
                # run_recon() has no output_dir default of its own (unlike run_debate/
                # run_build) -- it only writes codebase_context.md when explicitly given
                # one, so it must be passed here for the file to show up in the UI's
                # output panel.
                out_dir = Path(config.OUTPUT_DIR) / state.run_id
                recon_result = await run_recon(
                    sandbox_dir=sandbox_dir,
                    description=state.idea,
                    bus=state.bus,
                    output_dir=str(out_dir),
                    cost_state=cost_state,
                )
                # run_recon() never raises and always returns a usable context_text (a
                # graceful fallback note on failure) -- no "did Recon succeed" branching
                # needed here, per its own contract (agents/recon.py).
                blended_idea = (
                    f"{state.idea}\n\n=== CODEBASE CONTEXT (Recon) ===\n{recon_result['context_text']}"
                )
                debate_result = await run_debate(
                    idea=blended_idea,
                    num_rounds=state.num_rounds,
                    bus=state.bus,
                    run_id=state.run_id,
                    model=state.model,
                    effort=state.effort,
                    sandbox_dir=sandbox_dir,
                    cost_state=cost_state,
                )
                if debate_result.get("agreed_spec_path"):
                    await run_build(
                        run_id=state.run_id,
                        bus=state.bus,
                        model=state.model,
                        effort=state.effort,
                        target_mode=True,
                        diff_available=diff_available,
                        cost_state=cost_state,
                        allow_exec=state.allow_test_execution,
                    )
                else:
                    fatal_error = "Debate phase ended without an agreed spec; build phase skipped."
        else:
            debate_result = await run_debate(
                idea=state.idea,
                num_rounds=state.num_rounds,
                bus=state.bus,
                run_id=state.run_id,
                model=state.model,
                effort=state.effort,
                cost_state=cost_state,
            )
            if state.mode == "full":
                if debate_result.get("agreed_spec_path"):
                    await run_build(
                        run_id=state.run_id,
                        bus=state.bus,
                        model=state.model,
                        effort=state.effort,
                        cost_state=cost_state,
                        allow_exec=state.allow_test_execution,
                    )
                else:
                    fatal_error = "Debate phase ended without an agreed spec; build phase skipped."
            # mode == "debate_only": nothing further to do once run_debate returns --
            # a debate that ended without an agreed spec is still a legitimate (if
            # disappointing) end state for this mode, not a pipeline-level failure, so
            # unlike "full" it does not set fatal_error here.
    except AgentError as e:
        fatal_error = str(e)
    except asyncio.CancelledError:
        # Usage-Limit Resilience addon's cancel escape hatch: POST /run/{run_id}/cancel
        # calls state.task.cancel() (below), which delivers this at whatever await point
        # _run_pipeline is currently suspended -- most commonly inside a paused wait
        # (agents/runner.py's _wait_for_quota sleep), but a mid-call cancel is possible
        # too (agents/runner.py's own _run_once has matching CancelledError cleanup for
        # that case). Recorded as a clean, expected outcome -- not "internal error: ..." --
        # so the UI can label it "Cancelled" rather than "Finished with errors". Re-raised
        # after the finally block below runs its cleanup, so this task's own final state
        # is correctly "cancelled" rather than "completed" -- mirrors agents/runner.py's
        # own catch-cleanup-then-re-raise pattern for the same exception.
        fatal_error = "Run cancelled by user."
        raise
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

    if req.mode == "build_only":
        if not req.spec_text.strip():
            raise HTTPException(status_code=400, detail="spec_text must not be empty for mode=build_only")
    else:
        if not req.idea.strip():
            raise HTTPException(status_code=400, detail="idea must not be empty")
        if req.mode == "codebase" and not req.target_path.strip():
            raise HTTPException(status_code=400, detail="target_path must not be empty for mode=codebase")

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
        run_id=run_id,
        bus=bus,
        idea=req.idea,
        num_rounds=req.num_rounds,
        mode=req.mode,
        spec_text=req.spec_text,
        target_path=req.target_path,
        model=model,
        effort=effort,
        allow_test_execution=req.allow_test_execution,
    )
    runs[run_id] = state
    state.task = asyncio.create_task(_run_pipeline(state))
    return {"run_id": run_id}


@app.post("/run/{run_id}/cancel")
async def cancel_run(run_id: str) -> dict:
    """Usage-Limit Resilience addon's escape hatch: the pause/resume wait has no time
    cap by design (a subscription quota can take hours to reset), so this is the only
    way to stop a run short of killing the server. state.task is the same handle
    start_run() created -- cancelling it delivers asyncio.CancelledError at whatever
    await point the pipeline is currently suspended (most often mid-pause), which
    _run_pipeline's except asyncio.CancelledError clause turns into a clean "Cancelled"
    run_done instead of an internal-error-looking one."""
    state = runs.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown run_id")
    if state.task is not None:
        state.task.cancel()
    return {"cancelled": True}


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
            # Usage-Limit Resilience addon: this endpoint must answer quickly, not
            # potentially wait out a usage-limit reset -- a rate-limit hit here should
            # just report as unavailable like any other AgentError.
            wait_on_rate_limit=False,
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
        # Usage-Limit Resilience addon: True while a call is waiting out a usage-limit
        # exhaustion; paused_until is an ISO timestamp when a reset time was parseable,
        # else None (a fixed poll interval is used instead -- see runner.py).
        "paused": state.paused,
        "paused_until": state.paused_until,
    }


@app.get("/output/{run_id}")
async def list_output(run_id: str) -> dict:
    if run_id not in runs:
        raise HTTPException(status_code=404, detail="unknown run_id")
    run_dir = (OUTPUT_ROOT / run_id).resolve()
    if not run_dir.is_dir():
        return {"run_id": run_id, "files": []}
    # Codebase mode's sandbox (agents/sandbox.py) carries its own throwaway `.git/` for
    # diffing -- internal plumbing, not a user-facing output, and dumping its dozens of
    # hook/object files into the panel alongside patch.diff/build/*.py would be noise.
    files = sorted(
        str(rel).replace("\\", "/")
        for p in run_dir.rglob("*")
        if p.is_file() and ".git" not in (rel := p.relative_to(run_dir)).parts
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
