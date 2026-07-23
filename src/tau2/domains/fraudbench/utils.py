"""Paths for the FraudBench domain data."""

from tau2.utils.utils import DATA_DIR

FRAUDBENCH_DATA_DIR = DATA_DIR / "tau2" / "domains" / "fraudbench"
FRAUDBENCH_DB_PATH = FRAUDBENCH_DATA_DIR / "db.json"
FRAUDBENCH_TASK_SET_PATH = FRAUDBENCH_DATA_DIR / "tasks"
# Reserved for future fraud/AML policy docs; FraudBench currently reuses the
# banking_knowledge knowledge base (loaded by the delegated get_environment).
FRAUDBENCH_DOCUMENTS_DIR = FRAUDBENCH_DATA_DIR / "documents"
