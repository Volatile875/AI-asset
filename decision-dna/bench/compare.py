"""Run both pipelines and print the before/after comparison.

    python bench/compare.py

Each pipeline runs in its own subprocess so the `app` package resolves to the
right copy. Provider calls are simulated (see model.py) — counts are exact,
wall-clock is a model.
"""

import json
import subprocess
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
PY = sys.executable

ROWS = [
    ("LLM generations (total)", "llm_generations", "{}"),
    ("LLM max_tokens budget", "max_tokens_budget", "{}"),
    ("Prompt characters sent", "prompt_chars", "{}"),
    ("Prompt tokens (est, chars/4)", "prompt_tokens_est", "{}"),
    ("Embedding HTTP requests", "embedding_requests", "{}"),
    ("Texts embedded", "embedding_texts", "{}"),
    ("Pinecone queries", "pinecone_queries", "{}"),
    ("Chunks retrieved (sources basis)", "retrieved_chunks", "{}"),
    ("Chunks in the synthesis prompt", "context_chunks", "{}"),
    ("Wall clock, 1 query", "wall_clock_s", "{}s"),
    ("Wall clock, 4 concurrent", "concurrent_wall_clock_s", "{}s"),
    ("Sources returned", "sources", "{}"),
    ("Confidence returned", "confidence", "{}"),
]


def run(script, optional=False):
    proc = subprocess.run([PY, str(BENCH / script)], capture_output=True, text=True)
    if proc.returncode == 3 and optional:
        sys.stderr.write(proc.stderr)
        return None
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"{script} failed")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main():
    before = run("run_baseline.py", optional=True)
    after = run("run_optimized.py")

    if before is None:
        print("\nBaseline unavailable — reporting the current pipeline only.\n")
        for label, key, fmt in ROWS:
            print(f"{label:34s} {fmt.format(after[key]):>14s}")
        return

    print()
    print(f"{'Metric':34s} {'Before':>14s} {'After':>14s}   Change")
    print("-" * 84)
    for label, key, fmt in ROWS:
        b, a = before[key], after[key]
        if isinstance(b, (int, float)) and b and isinstance(a, (int, float)):
            delta = f"{(a - b) / b * 100:+.0f}%"
        else:
            delta = "same" if a == b else ""
        print(f"{label:34s} {fmt.format(b):>14s} {fmt.format(a):>14s}   {delta}")

    print("-" * 84)
    same_contract = before["response_keys"] == after["response_keys"]
    print(f"{'Response keys identical':34s} {str(same_contract):>31s}")
    print(f"{'processing_steps identical':34s} "
          f"{str(before['processing_steps'] == after['processing_steps']):>31s}")

    print()
    print("Critical path (generations that cannot overlap):")
    print(f"  before: {len(before['per_call'])} sequential  "
          f"{[c['max_tokens'] for c in before['per_call']]}")
    print("  after : 2 sequential — the planner generation now runs inside the")
    print("          retrieval branch, concurrently with the timeline branch.")
    print()
    print("Provider latency is simulated (model.py); read the ratios, not the seconds.")

    (BENCH / "results.json").write_text(
        json.dumps({"before": before, "after": after}, indent=2), encoding="utf-8")
    print(f"Raw metrics written to {BENCH / 'results.json'}")


if __name__ == "__main__":
    main()
