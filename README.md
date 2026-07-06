# Multi-Agent Debate & Build Pipeline

A local web app where you type a project idea, a team of AI agents debate and refine it into a
spec, and then a second team of agents builds it — real files, on disk. Every agent is a call to
the **Claude Code CLI** running on your machine. There's no API key, no billing dashboard, no
`anthropic` SDK anywhere in this repo — if you can run `claude` in your terminal, you can run this.

> This README is the pitch and the user-facing design doc — what the thing is and how it feels to
> use. For the full technical contract (CLI flags, event schema, endpoints, exact prompts), see
> [`SPEC.md`](./SPEC.md). For a running build log of what's actually been implemented so far, see
> [`progress.md`](./progress.md).

---

## Why

Claude Code is already a capable, file-writing, tool-using agent — and you're probably already
logged into it. This project treats the CLI itself as the only "model backend": one debate team
argues a rough idea into a real spec, one build team turns that spec into a working scaffold, and
you watch both happen live in a browser. No separate API integration to maintain, no second place
to manage auth or billing.

## How it works

1. You type a project idea into the web UI (e.g. *"a trading bot that uses an RSI strategy"*) and
   pick a number of debate rounds.
2. You hit **Run**. Two phases kick off, and you watch them happen in real time as color-coded
   agent cards streaming text token-by-token:

   **Phase 1 — Debate.** Three personas go back and forth for N rounds:
   - **Strategist** proposes the approach.
   - **Critic** is required to find at least one real flaw in every proposal — no polite
     agreement allowed.
   - **Refiner** calls things SETTLED and pushes the conversation forward instead of re-litigating.

   The debate ends with a **Refiner** synthesis pass that writes a structured `agreed_spec.md`
   (overview, requirements, architecture, file plan, key decisions, open risks).

   **Phase 2 — Build.** Four more agents take that spec and produce a real project:
   - **Architect** turns the spec into a concrete file tree.
   - **Coder** writes the actual source files itself, directly to disk (it's Claude Code — it has
     real file tools, so nobody has to scrape code blocks out of chat text).
   - **Reviewer** reads the code back off disk and writes a review.
   - **Tester** reads the review, writes real test files, and reports a test plan.

3. Everything lands in `output/<run-id>/`: the debate transcript, the agreed spec, the
   architecture doc, the review, the test plan, and a `build/` folder with the generated project.
   You can browse and click through the output files right in the UI.

## What this is *not*

- Not a code executor — generated code is written to disk but never run for you.
- Not multi-user — it's a single-user local tool (concurrent runs are fine; concurrent people
  aren't a design goal).
- Not another API wrapper — see the hard constraint below.

## Hard constraint: CLI only, never the API

All model calls go through the locally installed `claude` CLI via subprocess. This repo never
imports the `anthropic` SDK, never calls `api.anthropic.com`, and never reads an
`ANTHROPIC_API_KEY`. Auth is whatever you're already logged into with `claude`. If that's not
working, the app tells you so with the CLI's own error message instead of pretending to have its
own auth flow.

## Prerequisites

- Python 3.11+
- The [Claude Code CLI](https://docs.claude.com/en/docs/claude-code/overview) installed and
  already logged in — run `claude` once interactively first if you haven't. This app has no
  auth flow of its own; it's entirely riding on your existing CLI session.

## Quick start

```bash
pip install -r requirements.txt

# Sanity-check that the claude CLI is installed and logged in
python check_cli.py

# Run the app
uvicorn main:app
```

Then open the printed local URL, type an idea, and hit Run.

> **Don't use `--reload` on Windows.** Uvicorn's auto-reload supervisor changes how the asyncio
> event loop gets set up in a way that breaks `asyncio.create_subprocess_exec` — every agent call
> fails instantly with an empty `NotImplementedError` (surfaces in the UI as a bare "internal
> error:" with nothing after the colon). Plain `uvicorn main:app` runs fine; if you're iterating on
> backend code, just re-run that command after each change instead of using `--reload`.

### Headless smoke tests (useful while a stage is still being built)

```bash
python -m agents.runner "write a haiku about pipelines"   # stream a single agent call
python -m agents.debate "<idea>" --rounds 1                # run just the debate phase
python -m agents.build <run-id>                            # run just the build phase
```

## Project layout

```
main.py            FastAPI app entry point
config.py           All tunables: timeouts, round limits, model overrides, budget cap
check_cli.py        Preflight: is the CLI installed and authenticated?
agents/
  runner.py          The only place that shells out to the claude CLI
  events.py          Event bus (asyncio queue + replay) that both phases stream through
  debate.py          Phase 1: Strategist -> Critic -> Refiner loop
  build.py           Phase 2: Architect -> Coder -> Reviewer -> Tester
prompts/             The system prompt (persona) for every agent
static/              Plain HTML/CSS/JS frontend -- no build step
output/<run-id>/     Everything a run produces (gitignored)
```

## Status

This is being built stage-by-stage (scaffold -> runner -> debate -> build -> backend -> frontend ->
hardening), one stage per work session, each with its own pass/fail check before moving on. See
[`progress.md`](./progress.md) for exactly what's done, what decisions were made and why, and
what's left.

## Configuration

The knobs that matter live in `config.py`: default/max debate rounds, per-agent timeouts,
optional cheaper models for debate vs. build, a per-call budget cap passed straight to the CLI,
and how much debate history gets re-sent each round (full transcript for the first couple of
rounds, then a running summary + a short verbatim tail — token cost control, since every debate
call re-sends history). Nothing here is a secret; there's nothing to leak.
