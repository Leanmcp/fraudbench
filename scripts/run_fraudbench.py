#!/usr/bin/env python3
"""Run the public FraudBench task selection through the standard tau2 CLI."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    """Select public task IDs and forward model/run options to tau2."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-llm", default="gpt-4.1")
    parser.add_argument("--user-llm", default="gpt-4.1")
    parser.add_argument("--num-tasks", type=int, default=1, help="1–107; default: 1")
    parser.add_argument("--retrieval-config", default="bm25")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    path = ROOT / "data/curriculum/fraudbench_all.jsonl"
    ids = [
        json.loads(line)["id"] for line in path.read_text().splitlines() if line.strip()
    ]
    if not 1 <= args.num_tasks <= len(ids):
        parser.error(f"--num-tasks must be between 1 and {len(ids)}")
    command = [
        sys.executable,
        "-m",
        "tau2.cli",
        "run",
        "--domain",
        "fraudbench",
        "--agent-llm",
        args.agent_llm,
        "--user-llm",
        args.user_llm,
        "--retrieval-config",
        args.retrieval_config,
        "--num-trials",
        "1",
        "--max-steps",
        "50",
        "--task-ids",
        *ids[: args.num_tasks],
    ]
    if args.dry_run:
        import shlex

        print(shlex.join(command))
        return
    raise SystemExit(subprocess.call(command, cwd=ROOT))


if __name__ == "__main__":
    main()
