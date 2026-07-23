# FraudBench dataset

This directory contains the actual FraudBench dataset.

| Path | Contents |
|---|---|
| `tasks/` | 150 individual task JSON files, loaded by the benchmark |
| `tasks.json` | Compiled copy of the same 150 tasks |
| `db.json` | Synthetic banking database with FraudBench fixtures |
| `../../../curriculum/fraudbench_all.jsonl` | IDs of the 107 tasks selected for public evaluation |

The 150-task collection includes tasks outside the 107-task evaluation set.
Publishing this entire directory includes all 150 tasks.

FraudBench uses the shared banking policy corpus: 698 document JSON files in
`../banking_knowledge/documents/`. Include that corpus alongside this directory
and the evaluation task list when distributing the data needed for evaluation.
Running the benchmark also needs the banking environment, retrieval prompts,
and τ-bench code in this repository.

From the repository root, these scripts rebuild the generated files:

```bash
python scripts/build_fraudbench_db.py
python scripts/compile_fraudbench_tasks.py
```

Edit individual task files in `tasks/`, then rebuild `tasks.json`. The database
builder uses the shared banking database and adds synthetic FraudBench fixtures.
Tasks contain natural-language grading assertions evaluated over the simulated
conversation.
