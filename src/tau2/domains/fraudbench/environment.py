"""Environment + task loader for the FraudBench domain.

FraudBench reuses the banking_knowledge environment wholesale (tools, policy,
knowledge base) but swaps in the FraudBench database (banking DB + fraud
fixtures, produced by scripts/build_fraudbench_db.py). Tasks are loaded from
data/tau2/domains/fraudbench/tasks/ (one JSON file per task).
"""

import json
from pathlib import Path
from typing import Optional

from tau2.data_model.tasks import Task
from tau2.domains.banking_knowledge.data_model import TransactionalDB
from tau2.domains.banking_knowledge.environment import (
    get_environment as banking_get_environment,
)
from tau2.domains.fraudbench.utils import (
    FRAUDBENCH_DB_PATH,
    FRAUDBENCH_TASK_SET_PATH,
)
from tau2.environment.environment import Environment


def get_db() -> TransactionalDB:
    """Load the FraudBench transactional DB (banking DB + fraud fixtures)."""
    return TransactionalDB.load(str(FRAUDBENCH_DB_PATH))


def get_environment(
    db: Optional[TransactionalDB] = None,
    retrieval_variant: Optional[str] = None,
    retrieval_kwargs: Optional[dict] = None,
    task: Optional[Task] = None,
    solo_mode: bool = False,
) -> Environment:
    """Build the FraudBench environment.

    Delegates to banking_knowledge's get_environment (same tools, policy, and
    knowledge base) but with the FraudBench database. Signature matches banking
    so tau2_env's retrieval-variant/task forwarding works unchanged.
    """
    if db is None:
        db = get_db()
    return banking_get_environment(
        db=db,
        retrieval_variant=retrieval_variant,
        retrieval_kwargs=retrieval_kwargs,
        task=task,
        solo_mode=solo_mode,
    )


def get_tasks(task_split_name: Optional[str] = None) -> list[Task]:
    """Load FraudBench tasks from the per-task JSON files.

    Reads every *.json in the tasks directory and validates it as a Task.
    (task_split_name is accepted for registry compatibility but unused.)
    """
    tasks: list[Task] = []
    tasks_dir = Path(FRAUDBENCH_TASK_SET_PATH)
    if not tasks_dir.exists():
        return tasks
    for task_file in sorted(tasks_dir.glob("*.json")):
        try:
            with open(task_file, "r") as fp:
                task_data = json.load(fp)
            tasks.append(Task.model_validate(task_data))
        except Exception as e:
            print(f"Warning: Failed to load {task_file}: {e}")
    return tasks
