# Visualizer — what's new

A quick tour of the features added to the conversation view (the right-hand pane).
Open a run → pick a simulation, then use the tabs in the sim toolbar.

![Result Visualizer with the sim toolbar tabs and the task-explanation view](result-viz.png)

## Sim toolbar tabs

- **reasoning** — toggle the model's *thinking* on/off. When on, each turn shows a
  labelled **💭 USER THINKING** / **💭 ASSISTANT THINKING** block above the actual
  message — for *both* sides. (Comes from `reasoning_content` in the data; turns
  with no thinking just don't show a block.)
- **task** — the task's `description` and `evaluation_criteria` (the gold/expected
  actions + how it's graded). Structured fields now render as readable JSON instead
  of `[object Object]`.
- **reward json** — the score breakdown, shown two ways:
  - a **ticked checklist** like the terminal panel: `✅ #0 agent log_verification
    [write] 1.0`, green for pass / red for fail, with the expected call's arguments
    under each row, plus the **DB check** and a computed **partial reward** summary
    (e.g. `Action checks: 8/12 (66.7%) · write: 4/8`).
  - the full raw `reward_info` JSON below it.
- **agent prompt** — the agent's system prompt (the domain *policy*) + which LLM ran it.
- **user prompt** — the user-simulator's prompt: the **static** global guidelines
  *plus* **this task's scenario** (the per-task `user_scenario.instructions`), + the LLM.
- **kb docs** — the knowledge-base documents this task's answer depends on
  (its `required_documents`), shown in full, with the gold action(s) on top. This is
  the "why this is the correct answer" source material.
- **task explanation** — the human-written write-up for *this specific task*, parsed
  live from `CODE_EXPLANATION/BANKING_DATA_EXPLANATION/TASKS_EXPLANATION.md`
  (customer goal, traps, why it's correct, key source docs), rendered as formatted text.

## Other changes

- **Sort simulations** — a `sort:` bar above the sim list toggles between **file order**
  (as in the JSON) and **task id** (numeric, e.g. task_002 → task_006 → task_012). The
  `#N` labels stay the original sim index; only the order changes.
- Long prompts/docs now **wrap and scroll** in readable boxes (no more sideways scroll).
- Removed the old **filter messages** box.

## What needs a restart vs. a refresh

- UI-only changes (tabs rendering, sort, wrapping) → just **hard-refresh** the browser.
- Data changes (kb docs, task explanation, prompt text) come from `server.py`, so
  **restart the server** to pick them up:

  ```bash
  pkill -f server.py; ./run_visualizer_server.sh
  ```

## Where each thing comes from in the data

| Tab | Source |
|-----|--------|
| reasoning | `messages[].raw_data.choices[0].message.reasoning_content` |
| task | `tasks[].description` / `.evaluation_criteria` |
| reward json | `simulations[].reward_info` (`action_checks`, `db_check`, …) |
| agent prompt | `info.environment_info.policy` |
| user prompt | `info.user_info.global_simulation_guidelines` + task `user_scenario.instructions` |
| kb docs | `data/tau2/domains/<domain>/documents/<id>.json` (per `required_documents`) |
| task explanation | `CODE_EXPLANATION/<DOMAIN>_DATA_EXPLANATION/TASKS_EXPLANATION.md` |
