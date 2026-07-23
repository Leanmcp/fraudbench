#!/usr/bin/env python3
"""Archive known research clutter; default to a read-only preview."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "cleaning_script" / "archive"
# Explicit targets only: unknown files, source code, fixtures and credentials stay.
TARGETS = """
FAILURE_ANALYSIS
FIREWORKS_TRAINING
TAU2_WITH_TINKER
workspace
leanmcp_scripts
eval-protocol-edits
patches
RELEASE_NOTES.md
VERSIONING.md
.release-template.md
FRAUDBENCH
data/tau2/domains/fraudbench/FIX_PLAN.md
AAAI_2027_PLAN
ABSTRACTS
CLOUD_ESSENTIALS
DAILY_LOGS
LLM_KB
MOE_EXPLANATIONS
PREVIOUS_WORKS
RELATED_WORK
START_TINKER
exports
sort-tasks
sort-tasks-retail
FAILURE_ANALYSIS/analysis
leanmcp_scripts/eval_runs
leanmcp_scripts/workspace
TAU2_WITH_TINKER/evals_run
TAU2_WITH_TINKER/sim_runs
TAU2_WITH_TINKER/logs
TAU2_WITH_TINKER/notes
TAU2_WITH_TINKER/EXPERIMENTS
TAU2_WITH_TINKER/CODE_EXPLANATION
ALL_KB_DOCS.txt
ALL_KB_SUMMARIES.txt
ALL_KB_SUMMARY_ONLY.txt
ALL_KB_SUMMARY_SUCCINT.txt
ANSWER_SYSTEM_PROMPT_1.txt
ROUTER_SYSTEM_PROMPT_1.txt
openai_llm_index_prompt.txt
prompt_new.txt
LeanMCP_December_Investor_Update.md
LeanMCP_Investor_Update_June_2026.pdf
AUTOMATION_GUIDE.md
BEST_PRACTICES.md
CONCLUSIONS.md
DISCUSSION_IDEAS.md
POST_AAAI_PLANS.md
TODO.md
github-release-body.md
hash_compute.py
virtual_environment_setup.py
dir.tree
W20dir.tree
out.txt
view.txt
temp_var_row.json
trace_37.txt
traces.txt
tree
treecap.py
treecap.txt
data/dir.tree
data/treecap.txt
docs/agent_checklist_planning_research.md
docs/checklist_rl_brief_2026-08-01.md
docs/checklist_tool_vs_training_2026-07-27.md
docs/checklist_training_phase_plan.md
docs/next_steps_tool_and_training_2026-07-27.md
""".split()


def safe_path(base, relative):
    """Reject traversal and symlinks, including symlinked parent directories."""
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise ValueError(f"Unsafe relative path: {relative}")
    path = base
    for part in rel.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Refusing symlink: {path}")
    if not path.resolve().is_relative_to(ROOT):
        raise ValueError(f"Path outside repository: {path}")
    return path


def candidates(include_runs):
    """Build a deterministic, non-overlapping list of existing targets."""
    names = set(TARGETS)
    names.update(p.name for p in ROOT.glob("*.log"))
    if include_runs:
        # Keep the README and any loose inputs; archive run directories only.
        simulations = safe_path(ROOT, "data/simulations")
        if simulations.exists():
            names.update(
                str(p.relative_to(ROOT))
                for p in simulations.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
    result = []
    for name in sorted(names):
        path = safe_path(ROOT, name)
        if path.exists() and not any(parent in path.parents for parent in result):
            result.append(path)
    return result


def restore(run_name):
    """Restore an archive without overwriting any existing path."""
    if Path(run_name).name != run_name:
        raise ValueError("Restore expects a single archive directory name")
    run = safe_path(ARCHIVE, run_name)
    manifest = json.loads((run / "manifest.json").read_text())
    pairs = []
    for name in manifest["paths"]:
        source = safe_path(run / "files", name)
        dest = safe_path(ROOT, name)
        if source.exists():
            if dest.exists():
                raise ValueError(f"Restore would overwrite {dest}")
            pairs.append((source, dest))
    for source, dest in pairs:
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.rename(dest)
    print(f"Restored {len(pairs)} paths from {run}")


def main():
    """Preview, archive, or restore repo-local cleanup targets."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="Move targets to local archive"
    )
    mode.add_argument("--restore", metavar="ARCHIVE_NAME")
    parser.add_argument(
        "--include-simulation-runs",
        action="store_true",
        help="Also archive data/simulations run directories (including FraudBench results)",
    )
    args = parser.parse_args()
    if not (ROOT / "src/tau2").is_dir() or not (ROOT / "pyproject.toml").is_file():
        parser.error("Script must live in this repository's cleaning_script directory")
    if args.restore:
        restore(args.restore)
        return
    paths = candidates(args.include_simulation_runs)
    for path in paths:
        print(f"ARCHIVE {path.relative_to(ROOT)}")
    print(f"\n{len(paths)} paths selected.")
    if not args.apply:
        print(
            "Preview only. Use --apply to move these paths into cleaning_script/archive/."
        )
        return
    if not paths:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run = safe_path(ROOT, f"cleaning_script/archive/{stamp}")
    run.mkdir(parents=True, exist_ok=False)
    # Write the full plan first; interrupted moves can be restored from this manifest.
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "paths": [str(p.relative_to(ROOT)) for p in paths],
            },
            indent=2,
        )
        + "\n"
    )
    for source in paths:
        dest = run / "files" / source.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.rename(dest)
    print(f"Archived inside {run.relative_to(ROOT)}")
    print(f"Restore: python3 cleaning_script/clean_repo.py --restore {stamp}")


if __name__ == "__main__":
    main()
