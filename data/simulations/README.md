# simulations/

Output of tau2-bench benchmark runs. Each run produces a `results.json` containing the run
config, the task definitions, and one simulation per task (full conversation, tool calls,
and reward/evaluation).

## Layout

Two naming conventions live here:

- **Legacy (flat):** `<timestamp>_<domain>_llm_agent_<model>_user_simulator_<model>/results.json`
- **Leaderboard:** `leaderboard/run_<timestamp>/<model>__<domain>/results.json`

`<domain>` is the benchmark task set (e.g. `banking_knowledge`, `retail`).

## Inside a `results.json`

| Key | Meaning |
|-----|---------|
| `info` | Run config: agent/user model, domain, policy, trials, steps. |
| `tasks` | Task definitions: description, evaluation criteria, scenario. |
| `simulations` | One run per task: `messages`, `reward_info`, `termination_reason`. |
| `timestamp` | When the run was produced. |

`reward` (in `simulations[].reward_info`) is the per-task score — `1.0` pass, `0.0` fail,
`null` if the run ended without evaluation.

## Viewing results

These files are large (tens of MB) and hang editors. Use the browser/CLI tool instead:

```bash
cd ../../result-visualizer && ./run_visualizer_server.sh   # http://localhost:8765
```

See `result-visualizer/README.md`.
