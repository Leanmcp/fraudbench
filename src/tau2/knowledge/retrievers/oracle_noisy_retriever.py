"""Oracle retriever with controlled distractor injection ("noise").

Stage 0 of the cluster-granularity capacity study (see
``AAAI_2027_PLAN/INDEXING_TRACK/CLUSTER_GRANULARITY_REVIEW.md``): isolates
raw "how many documents can this model handle in one KB_search result"
sensitivity from retrieval-mechanism quality, by starting from a perfect
oracle result (the task's ``required_documents``) and padding it with N
additional, deliberately-irrelevant documents before the agent ever sees it.

Two distractor modes, sourced by ``oracle_noisy_pipeline.py``'s cluster-aware
sampling (stashed into pipeline state as ``noise_doc_refs``):

- ``same_cluster``: distractors are drawn from the same topic cluster(s) as
  the required documents (e.g. other checking-account products) — this is
  the condition that should reproduce the "near-duplicate product name
  collision" failure mode found repeatedly during the independent cluster
  read-through (Green/Blue/Silver family confusion).
- ``cross_cluster``: distractors are drawn from unrelated clusters — this
  isolates raw context-volume/attention-dilution sensitivity from
  topical-similarity confusion.

Required docs and noise docs are combined and shuffled (seeded, for
reproducibility) so the agent cannot distinguish "the real answer" from
"the padding" by position alone — the whole point is to force it to actually
read and reason rather than pattern-match on retrieval order.
"""

import logging
import random
from typing import Any, Dict, List, Optional, Tuple

from tau2.knowledge.registry import register_retriever
from tau2.knowledge.retrievers.base import BaseRetriever

logger = logging.getLogger(__name__)


@register_retriever("oracle_noisy")
class OracleNoisyRetriever(BaseRetriever):
    """Returns the task's ground-truth documents PLUS N distractor documents.

    The pipeline state must contain:
    - ``required_doc_refs``: the task's true ``required_documents`` (ids).
    - ``noise_doc_refs``: the pre-sampled distractor document ids (may be
      empty — noise_n=0 makes this behave identically to ``OracleRetriever``).

    All returned documents get score 1.0 (order is the only signal, and
    order is shuffled) — a downstream postprocessor cannot tell required
    docs from noise docs by score, only the agent's own reasoning can.
    """

    def __init__(
        self,
        required_refs_state_key: str = "required_doc_refs",
        noise_refs_state_key: str = "noise_doc_refs",
        content_state_key: str = "doc_content_map",
        title_state_key: str = "doc_title_map",
        shuffle_seed: Optional[int] = None,
        **kwargs,
    ):
        super().__init__(
            required_refs_state_key=required_refs_state_key,
            noise_refs_state_key=noise_refs_state_key,
            content_state_key=content_state_key,
            title_state_key=title_state_key,
            shuffle_seed=shuffle_seed,
            **kwargs,
        )
        self.required_refs_state_key = required_refs_state_key
        self.noise_refs_state_key = noise_refs_state_key
        self.content_state_key = content_state_key
        self.title_state_key = title_state_key
        self.shuffle_seed = shuffle_seed

    def retrieve(
        self, input_data: Dict[str, Any], state: Dict[str, Any]
    ) -> List[Tuple[str, float]]:
        required_refs = state.get(self.required_refs_state_key) or []
        noise_refs = state.get(self.noise_refs_state_key) or []

        if not required_refs:
            logger.warning(
                "OracleNoisyRetriever: no required documents in pipeline "
                f"state (key {self.required_refs_state_key!r}). Was the "
                "pipeline built with a task? Returning no results."
            )
            return []

        content_map = state.get(self.content_state_key, {})
        title_to_id = {
            title: doc_id
            for doc_id, title in state.get(self.title_state_key, {}).items()
        }

        def _resolve(ref: str) -> Optional[str]:
            if ref in content_map:
                return ref
            return title_to_id.get(ref)

        combined_refs = list(required_refs) + list(noise_refs)
        if self.shuffle_seed is not None:
            rng = random.Random(self.shuffle_seed)
            rng.shuffle(combined_refs)

        results: List[Tuple[str, float]] = []
        seen: set = set()
        for ref in combined_refs:
            doc_id = _resolve(ref)
            if doc_id is None:
                logger.warning(
                    f"OracleNoisyRetriever: document not found in KB: {ref!r}"
                )
                continue
            if doc_id in seen:
                continue
            seen.add(doc_id)
            results.append((doc_id, 1.0))

        return results
