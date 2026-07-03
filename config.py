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
BUILD_MAX_TURNS = 30          # --max-turns for builder-mode agents

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
