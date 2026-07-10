"""All tunables for the multi-agent pipeline. No secrets live here — auth is the Claude CLI's job."""

DEFAULT_DEBATE_ROUNDS = 3
MAX_DEBATE_ROUNDS = 8

# Timeouts (seconds) — deliberately different per agent type
DEBATE_TIMEOUT = 180          # one debate speech
ARCHITECT_TIMEOUT = 300
CODER_TIMEOUT = 900           # writes a whole project; needs headroom
REVIEWER_TIMEOUT = 600
TESTER_TIMEOUT = 600
BUGFIXER_TIMEOUT = 600

# CLI guards
DEBATE_MODEL = None           # None = CLI default; can set e.g. a cheaper model name
BUILD_MODEL = None
MAX_BUDGET_USD_PER_CALL = 1.0  # passed as --max-budget-usd

# Curated model aliases offered in the browser UI's dropdown (main.py's /run and
# /models/check accept any string, not just these -- this list is just what's
# offered by default; a user can still type/POST a newer alias or full model name
# as new models ship). Not validated against server-side: /models/check exists
# precisely so an unlisted or unavailable name gets a real answer instead of a
# guess. Mirrors the CLI's own --model help text (aliases for the latest models).
AVAILABLE_MODELS = ["sonnet", "opus", "haiku", "fable"]

# The CLI's --effort choices verbatim (confirmed via `claude --help`). Unlike an
# invalid --model (which the CLI rejects with a clear, free, fast error), an
# invalid --effort value is silently ignored with just a stderr warning -- so
# this list is validated server-side in main.py (400 on a bad value) rather than
# relying on the CLI to catch it.
AVAILABLE_EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
# --max-turns for builder-mode agents. 30 proved tight for write-heavy sessions
# (Coder/Tester reading several files, editing, and reasoning about each) and was
# observed hitting "error_max_turns" after streaming an apparently complete reply --
# see agents/runner.py's handling of that subtype.
BUILD_MAX_TURNS = 60

# A single stream-json line can embed a whole tool result (e.g. the Reviewer/Tester
# reading a source file back with their file tools) -- easily past asyncio's 64 KiB
# StreamReader default, which raises ValueError/LimitOverrunError mid-run.
STREAM_READ_LIMIT_BYTES = 20 * 1024 * 1024  # 20 MiB

OUTPUT_DIR = "./output"
HISTORY_FULL_ROUNDS = 2       # rounds that get the full transcript
HISTORY_TAIL_MESSAGES = 6     # verbatim tail after that

# Agent registry: name -> (prompt file, mode, timeout)
PROMPTS_DIR = "./prompts"

DEBATE_AGENTS = [
    ("Strategist", "debate/strategist.txt"),
    ("Critic", "debate/critic.txt"),
    ("Refiner", "debate/refiner.txt"),
]
REFINER_FINAL_INSTRUCTION = "debate/refiner_final.txt"

BUILD_AGENTS = {
    "Architect": ("build/architect.txt", "text_only", ARCHITECT_TIMEOUT),
    "Coder": ("build/coder.txt", "builder", CODER_TIMEOUT),
    "Reviewer": ("build/reviewer.txt", "builder", REVIEWER_TIMEOUT),
    "Tester": ("build/tester.txt", "builder", TESTER_TIMEOUT),
    # Stage 15: only ever run when allow_exec is True (agents/build.py's run_build()) --
    # not part of the default four-step pipeline, so it's looked up directly rather than
    # iterated over generically the way the other four entries conceptually are.
    "BugFixer": ("build/bugfixer.txt", "builder_exec", BUGFIXER_TIMEOUT),
}

# Test Execution addon (SPEC.md v6), Stage 14. Tester runs in "builder_exec" mode
# (agents/runner.py) instead of plain "builder" only when a run explicitly opts in
# (POST /run's allow_test_execution, default False) -- see agents/build.py's run_build().
# Separate turn budget from BUILD_MAX_TURNS: write tests + exactly one test-command run,
# not a write-heavy multi-file session.
TESTER_MAX_TURNS = 40

# Stage 15: BugFixer's own turn budget -- fix + exactly one verification re-run of the
# test command, same rationale as TESTER_MAX_TURNS above (not a write-heavy multi-file
# session, and never an iteration loop).
BUGFIXER_MAX_TURNS = 40

# Confirmed empirically against the installed CLI (three real, separate mechanisms, not
# one -- see SPEC.md's Test Execution addon for the full trail):
#   1. --tools is the structural gate: Bash absent from --tools means the tool doesn't
#      exist for the model at all, regardless of permission mode.
#   2. --permission-mode acceptEdits auto-approves *some* Bash commands by default (a
#      real internal risk classifier judges these, not a flag we control) -- trivial/
#      read-only-looking commands (echo, whoami, mkdir) passed with zero denial in
#      testing, but commands that look consequential (pip install, running a test
#      suite) got denied even under acceptEdits, with no one able to approve them
#      non-interactively. --allowedTools genuinely fixes this: a command matching an
#      allow pattern (confirmed with a real call: "Bash(pip install*)"/
#      "Bash(python -m pytest*)") pre-approves it past that classifier.
#   3. --disallowedTools genuinely blocks a matching prefix regardless of the above --
#      confirmed with a real call ("Bash(git push*)" denied "git push origin main
#      --dry-run" while an unrelated allowed command still ran in the same session).
# So builder_exec combines all three: Bash only exists via --tools, a curated allowlist
# of common install/test-runner invocations (below) so the classifier doesn't silently
# swallow the one thing this mode exists for, and a denylist of known-dangerous prefixes
# as a hard backstop that still applies on top. This is still not a hard sandbox --
# anything not on the denylist and not requiring classifier approval can run -- layered
# with cwd confinement to the disposable build/sandbox dir, a tight max-turns budget, and
# prompt-level instruction (run the documented command once, never iterate).
BUILD_EXEC_ALLOWED_TOOLS = [
    # Python
    "Bash(pip install*)", "Bash(pip3 install*)", "Bash(python -m pip install*)",
    "Bash(pytest*)", "Bash(python -m pytest*)", "Bash(python -m unittest*)",
    # Node/JS
    "Bash(npm install*)", "Bash(npm ci*)", "Bash(npm test*)", "Bash(npm run test*)",
    "Bash(yarn install*)", "Bash(yarn test*)", "Bash(pnpm install*)", "Bash(pnpm test*)",
    # Go / Rust
    "Bash(go test*)", "Bash(go build*)", "Bash(go mod download*)", "Bash(go mod tidy*)",
    "Bash(cargo test*)", "Bash(cargo build*)",
    # JVM
    "Bash(mvn test*)", "Bash(mvn install*)", "Bash(gradle test*)", "Bash(./gradlew test*)",
    # .NET / Ruby
    "Bash(dotnet test*)", "Bash(dotnet restore*)", "Bash(dotnet build*)",
    "Bash(bundle install*)", "Bash(rspec*)", "Bash(rake test*)",
]

BUILD_EXEC_DISALLOWED_TOOLS = [
    # Destructive filesystem ops
    "Bash(rm *)", "Bash(rd *)", "Bash(rmdir *)", "Bash(del *)", "Bash(erase *)",
    "Bash(format *)", "Bash(mkfs*)",
    # Network / remote access
    "Bash(curl *)", "Bash(wget *)", "Bash(nc *)", "Bash(netcat *)",
    "Bash(ssh *)", "Bash(scp *)", "Bash(sftp *)", "Bash(ftp *)",
    # Git operations that could discard history or reach a remote
    "Bash(git push*)", "Bash(git reset --hard*)", "Bash(git clean*)",
    # Privilege / process / system control
    "Bash(sudo *)", "Bash(su *)", "Bash(shutdown*)", "Bash(reboot*)",
    "Bash(taskkill*)", "Bash(kill *)", "Bash(Stop-Process*)",
    # Permission/ownership changes
    "Bash(chmod *)", "Bash(chown *)", "Bash(icacls*)", "Bash(takeown*)",
]

# Codebase Analysis Mode (SPEC.md addon), Stage 7: sandbox preparation.
# Directories/files excluded when copying the user's real codebase into the
# sandbox -- matched by name at every tree level via shutil.ignore_patterns.
SANDBOX_IGNORE = [
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", "target", ".mypy_cache", ".pytest_cache",
]

# Identity for the throwaway baseline commit made *inside* the sandbox copy --
# never the user's own identity, and never written to their global git config.
# Passed inline via `git -c ...` for that single commit only; see agents/sandbox.py.
SANDBOX_GIT_NAME = "debate-pipeline"
SANDBOX_GIT_EMAIL = "debate-pipeline@localhost"

# Codebase Analysis Mode, Stage 8: the Recon agent. read_only mode grants Read/Glob/
# Grep only (agents/runner.py) -- structural enforcement, not just prompt discipline.
RECON_TIMEOUT = 300   # read-only exploration, not a write-heavy session
RECON_MAX_TURNS = 20  # lower than BUILD_MAX_TURNS -- no edits to make, just look
RECON_MODEL = None    # None = CLI default; independently tunable from DEBATE_MODEL/BUILD_MODEL

RECON_AGENT = ("codebase/recon.txt", "read_only", RECON_TIMEOUT)  # (prompt file, mode, timeout)

# Codebase Analysis Mode, Stage 10: patch-build. Same mode/timeout as the regular Coder
# (config.BUILD_AGENTS["Coder"]) -- only the persona differs, so this is just the one
# prompt-file override run_build() swaps in when target_mode=True, not a whole new
# BUILD_AGENTS entry.
CODER_PATCH_PROMPT_FILE = "codebase/coder_patch.txt"

# Usage-Limit Resilience addon: pause + resume a call instead of failing it when a
# subscription session/weekly quota is exhausted mid-run (as opposed to a genuine model/
# CLI error, which still goes through the existing retry-once-then-fail path unchanged).
# This is a deliberate, narrow exception to the "no rate-limit/backoff engineering" line
# in SPEC.md's Design Decision section -- see SPEC.md's Error Handling section and the
# addon section for the reasoning. Only the CLI's user-facing message *wording* is
# documented (not the NDJSON envelope it arrives in), so detection is substring matching
# across whatever text is available (result/stderr/rate_limit_event) -- these markers
# live here, not hardcoded in agents/runner.py, so a future wording change is a one-line
# config fix instead of a code change.
RATE_LIMIT_MARKERS = [
    "hit your session limit",
    "hit your weekly limit",
    "hit your opus limit",
    "usage limit reached",  # generic fallback in case the exact wording drifts
]
RATE_LIMIT_TRANSIENT_MARKER = "request rejected (429)"  # a transient API-tier rate limit,
# distinct from a subscription quota -- the CLI already retries these internally
# (system/api_retry events), so this is only a safety net if one still bubbles up as a
# terminal failure; it gets a short fixed backoff, not the long quota-reset wait below.

RATE_LIMIT_POLL_SECONDS = 900       # fallback wait when no reset time is parseable (15 min)
RATE_LIMIT_HEARTBEAT_SECONDS = 30   # how often a `paused` heartbeat event re-emits while waiting
API_429_BACKOFF_SECONDS = 30        # short backoff for RATE_LIMIT_TRANSIENT_MARKER

# Local-only HTTP guard (main.py's LocalOnlyMiddleware): this server is an unauthenticated
# single-user tool, and starting a run costs real subscription quota (and, with
# allow_test_execution, executes code) -- so every request must look like it comes from
# this machine's own user, not a webpage the user happens to have open. Two checks, both
# against this hostname allowlist (port ignored):
#   - Host header: DNS-rebinding defense, and the primary reason this exists. POST /run
#     only parses its JSON body under Content-Type: application/json, which is not a
#     CORS-"simple" type -- so a plain cross-origin fetch triggers a preflight this server
#     never answers. But an attacker who rebinds their domain to 127.0.0.1 becomes
#     same-origin (no preflight, body parsed); the rebound request carries the attacker's
#     domain in Host, so this allowlist is what rejects it.
#   - Origin header (when present): CSRF defense-in-depth for mutating endpoints that read
#     no JSON body (e.g. POST /run/{id}/cancel), which a cross-origin simple request can
#     otherwise reach without a preflight. A browser always stamps such a request with the
#     initiating page's Origin; the UI's own same-origin requests carry a local one, and
#     curl/scripts send none at all and pass untouched.
# Deliberately serving on a LAN interface (uvicorn --host 0.0.0.0)? Add that hostname/IP here.
TRUSTED_HOSTS = ["localhost", "127.0.0.1", "::1"]
