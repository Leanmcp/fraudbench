# Repository cleanup

Run from the repository root (Python 3.12+, no extra dependencies):

```bash
# Preview the exact paths; changes nothing.
python3 cleaning_script/clean_repo.py

# Move selected clutter into an ignored archive INSIDE this repository.
python3 cleaning_script/clean_repo.py --apply

# Optionally include saved simulation runs, including FraudBench results.
python3 cleaning_script/clean_repo.py --include-simulation-runs
python3 cleaning_script/clean_repo.py --include-simulation-runs --apply

# Undo using the archive name printed by --apply.
python3 cleaning_script/clean_repo.py --restore ARCHIVE_NAME
```

The script targets personal research plans, investor updates, downloaded related
work, standalone tutorials, task-sorting experiments, root logs, and historical
training/analysis outputs. The explicit target list is in `clean_repo.py`.
This is a scope-based cleanup, not a claim that every removed experiment is
irreproducible. Unknown files are preserved.

It preserves:

- `src/`, `tests/`, all domain data, voice fixtures, packaging, lockfiles, license,
  τ-bench docs/examples, web UI and result visualizer.
- FraudBench domain data, curricula, dataset builders in `scripts/`, and saved
  simulation results by default.
- Banking knowledge data and `PROMPT_LIBRARY`, used by FraudBench retrieval.

The legacy training/evaluation directories are archived. The minimal example
in `scripts/run_fraudbench.py` now uses the standard tau2 runner directly.

- `.git`, `.env`, local environments, and pre-existing user changes.

Archives retain original relative paths and a manifest. Restore refuses to
replace existing files; move conflicting files yourself before retrying.
Interrupted archive operations can be restored using the same manifest.
No command writes outside this repository, permanently deletes files, changes
Git history, commits, pushes, or publishes anything. Archives still use disk space.

Before publishing, review `git diff --stat` and stage intended deletions explicitly.
The ignored archive must not be force-added or included in a source ZIP. Cleanup
of the working tree does not remove historical files from Git history. This
script alone does not certify the repository as ready for public release.

After applying, validate the retained project:

```bash
uv sync --extra dev --extra knowledge
uv run tau2 check-data
make test
make check-all
```

Live evaluation needs provider credentials and is not run by this script.

The uppercase `FRAUDBENCH/` paper/planning directory and the dataset's
`FIX_PLAN.md` are archived too. Actual data stays in `data/tau2/domains/fraudbench/`.
