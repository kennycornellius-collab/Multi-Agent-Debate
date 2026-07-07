"""All tunables for the multi-agent pipeline. No secrets live here — auth is the Claude CLI's job."""

DEFAULT_DEBATE_ROUNDS = 3
MAX_DEBATE_ROUNDS = 8

# Timeouts (seconds) — deliberately different per agent type
DEBATE_TIMEOUT = 180          # one debate speech
ARCHITECT_TIMEOUT = 300
CODER_TIMEOUT = 900           # writes a whole project; needs headroom
REVIEWER_TIMEOUT = 600
TESTER_TIMEOUT = 600

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
}

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
