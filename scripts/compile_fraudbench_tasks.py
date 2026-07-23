"""Compile individual FraudBench tasks into tasks.json.

Run from the repository root: python scripts/compile_fraudbench_tasks.py
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRAUDBENCH_DIR = REPO_ROOT / "data" / "tau2" / "domains" / "fraudbench"
TASKS_DIR = FRAUDBENCH_DIR / "tasks"
OUT = FRAUDBENCH_DIR / "tasks.json"


def main() -> int:
    files = sorted(TASKS_DIR.glob("*.json"))
    if not files:
        print(f"ERROR: no task files found in {TASKS_DIR}")
        return 1

    tasks = []
    ids = set()
    for f in files:
        with f.open() as fh:
            task = json.load(fh)
        tid = task.get("id")
        if not tid:
            print(f"ERROR: {f.name} has no 'id'")
            return 1
        if tid in ids:
            print(f"ERROR: duplicate task id '{tid}' ({f.name})")
            return 1
        ids.add(tid)
        tasks.append(task)

    tasks.sort(key=lambda t: t["id"])
    with OUT.open("w") as fh:
        json.dump(tasks, fh, indent=2)
        fh.write("\n")

    print(f"Wrote {len(tasks)} task(s) to {OUT}")
    for t in tasks:
        print(f"  - {t['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
