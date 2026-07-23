# FraudBench

FraudBench is a dataset of adversarial banking conversations evaluated with τ-bench.

## Data

- [`data/tau2/domains/fraudbench/tasks/`](data/tau2/domains/fraudbench/tasks/): 150 task JSON files.
- [`data/tau2/domains/fraudbench/tasks.json`](data/tau2/domains/fraudbench/tasks.json): the same tasks in one file.
- [`data/tau2/domains/fraudbench/db.json`](data/tau2/domains/fraudbench/db.json): synthetic banking database.
- [`data/curriculum/fraudbench_all.jsonl`](data/curriculum/fraudbench_all.jsonl): the 107-task public evaluation selection.
- [`data/tau2/domains/banking_knowledge/documents/`](data/tau2/domains/banking_knowledge/documents/): 698 shared policy documents.

The complete task directory includes all 150 tasks, including those outside the
107-task evaluation selection. Keep the shared banking data and prompts to run it.

## Run one task

Requires Python 3.12 or 3.13 and `uv`:

```bash
uv sync --extra knowledge
export OPENAI_API_KEY="your-key"
uv run python scripts/run_fraudbench.py
```

This runs a `gpt-4.1` defender and caller, using BM25 policy search. The benchmark
uses its configured natural-language assertion judge (`gpt-5.4-nano` in
`src/tau2/config.py`), which also needs the OpenAI key. Model calls incur API costs.
BM25 needs no embedding API or shell sandbox. This minimal example uses different
retrieval settings from earlier all-tools experiments.

```bash
# Evaluate all 107 selected tasks; change either model with LiteLLM model IDs.
uv run python scripts/run_fraudbench.py --num-tasks 107

# Inspect the command without making model calls.
python3 scripts/run_fraudbench.py --dry-run
```

Equivalent direct command for one task:

```bash
uv run tau2 run --domain fraudbench --retrieval-config bm25 \
  --agent-llm gpt-4.1 --user-llm gpt-4.1 \
  --task-ids fb_task_appscam_01 --num-trials 1 --max-steps 50
```

Results are saved under `data/simulations/`. Use `uv run tau2 view` to inspect them.
Without explicit task IDs, the FraudBench loader exposes all 150 tasks.

## Framework

`src/tau2/`, `tests/`, and the other domain data retain the τ-bench framework.
See [CONTRIBUTING.md](CONTRIBUTING.md) for development and [LICENSE](LICENSE)
for the license. The example runs entirely through the standard τ-bench runner;
no separate training checkout is required.

## Cleanup and verification

The uppercase `FRAUDBENCH/` paper folder and legacy training/research folders
have been removed from the active tree. The dataset remains in
`data/tau2/domains/fraudbench/`. The local cleanup archive has been deleted. The install no longer depends on
a sibling repository checkout.

During cleanup, 100 offline tests passed, and FraudBench data loading, BM25
environment construction, and standard runner construction passed. No paid
model evaluation was run. Preview the example without API calls using:

```bash
python3 scripts/run_fraudbench.py --dry-run
```

A GitHub credential was removed from `requirements.txt`. The old local Git
history has been removed. Its owner should still revoke the previously exposed
credential; other copies of the old history may exist.
