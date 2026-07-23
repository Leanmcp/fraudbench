"""Oracle retriever that returns a task's ground-truth documents directly.

Ignores the query entirely and returns the documents listed in the current
task's ``required_documents`` (stashed in pipeline state by
``create_oracle_retrieval_pipeline``). Used to measure a model's reasoning
in isolation: retrieval is perfect by construction, so any remaining task
failures cannot be blamed on retrieval errors.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from tau2.knowledge.registry import register_retriever
from tau2.knowledge.retrievers.base import BaseRetriever

logger = logging.getLogger(__name__)


@register_retriever("oracle")
class OracleRetriever(BaseRetriever):
    """Returns the ground-truth documents for the current task.

    The pipeline state must contain ``required_doc_refs``: the task's
    ``required_documents`` list (document ids or titles — both resolve).
    All docs get score 1.0, in the order they appear in the task file.

    ``max_docs=None`` (the default) returns ALL required documents —
    there is deliberately no top_k, since truncating an oracle would
    reintroduce retrieval errors.
    """

    def __init__(
        self,
        max_docs: Optional[int] = None,
        required_refs_state_key: str = "required_doc_refs",
        content_state_key: str = "doc_content_map",
        title_state_key: str = "doc_title_map",
        **kwargs,
    ):
        super().__init__(
            max_docs=max_docs,
            required_refs_state_key=required_refs_state_key,
            content_state_key=content_state_key,
            title_state_key=title_state_key,
            **kwargs,
        )
        self.max_docs = max_docs
        self.required_refs_state_key = required_refs_state_key
        self.content_state_key = content_state_key
        self.title_state_key = title_state_key

    def retrieve(
        self, input_data: Dict[str, Any], state: Dict[str, Any]
    ) -> List[Tuple[str, float]]:
        state["oracle_retriever_active"] = True
        state["oracle_call_count"] = state.get("oracle_call_count", 0) + 1

        refs = state.get(self.required_refs_state_key)
        if not refs:
            logger.warning(
                "OracleRetriever: no required documents in pipeline state "
                f"(key {self.required_refs_state_key!r}). Was the pipeline "
                "built with a task? Returning no results."
            )
            return []

        content_map = state.get(self.content_state_key, {})
        # required_documents entries may be ids or titles (same as golden_prompt)
        title_to_id = {
            title: doc_id
            for doc_id, title in state.get(self.title_state_key, {}).items()
        }

        results: List[Tuple[str, float]] = []
        for ref in refs:
            doc_id = ref if ref in content_map else title_to_id.get(ref)
            if doc_id is None:
                logger.warning(
                    f"OracleRetriever: required document not found in KB: {ref!r}"
                )
                continue
            results.append((doc_id, 1.0))

        if self.max_docs is not None:
            results = results[: self.max_docs]
        return results
