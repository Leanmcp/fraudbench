"""LLM-built index cards: per-document summary + FAQs, disk-cached.

Runs as a document_preprocessor inside RetrievalPipeline.index_documents().
The environment (and thus the pipeline) is rebuilt once per episode AND
again at reward time, so every card is cached on disk keyed by
(doc content hash, model, prompt version): the LLM is only called for
documents it has never seen (or whose content changed) — indexing happens
lazily on first use, and a new/changed doc gets its card built and appended
automatically on the next run.

Index folder layout (human-inspectable JSON, one file per document):
    data/llm_kb_index/<model>_<prompt_version>/<doc_id>_<hash>.json
"""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from tau2.knowledge.document_preprocessors.base import BaseDocumentPreprocessor
from tau2.knowledge.llm_client import KBLLMClient
from tau2.knowledge.registry import register_document_preprocessor
from tau2.utils.utils import DATA_DIR

# Bump when INDEXING_SYSTEM_PROMPT changes so stale cards are recomputed.
INDEX_PROMPT_VERSION = "v1"

DEFAULT_INDEX_DIR = DATA_DIR / "llm_kb_index"
DEFAULT_MAX_CONCURRENCY = 16

INDEXING_SYSTEM_PROMPT = """\
You are building a retrieval index card for one document from a bank's
internal knowledge base. Customer-support agents will use these cards to
decide which documents to open, so the card must make the document's
coverage obvious at a glance.

Write:
1. summary: 1-2 sentences (max ~40 words) saying exactly what this
   document covers — name the products, fees, limits, procedures, and
   numbers it contains. Be concrete, not generic.
2. faqs: 3-6 short questions a customer or agent would ask that THIS
   document (and ideally only this document) answers.
3. keywords: 5-12 distinctive terms/phrases from the document (product
   names, fee names, form names, dollar amounts, timeframes).

Do not invent information that is not in the document.
"""

# In-memory cache so the many per-episode environment rebuilds within one
# eval process don't even touch the disk cache. Keyed like the disk cache.
_MEM_CACHE: Dict[str, Dict[str, Any]] = {}


class DocIndexCard(BaseModel):
    summary: str = Field(description="1-2 sentence summary of the document")
    faqs: List[str] = Field(description="3-6 questions this document answers")
    keywords: List[str] = Field(description="5-12 distinctive terms")


@register_document_preprocessor("llm_indexer")
class LLMIndexer(BaseDocumentPreprocessor):
    def __init__(
        self,
        model: Optional[str] = None,
        state_key: str = "llm_index",
        index_dir: Optional[str] = None,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        **kwargs,
    ):
        super().__init__(
            model=model,
            state_key=state_key,
            index_dir=index_dir,
            max_concurrency=max_concurrency,
            **kwargs,
        )
        self.client = KBLLMClient(model=model)
        self.state_key = state_key
        self.max_concurrency = max_concurrency
        base_dir = Path(index_dir) if index_dir else DEFAULT_INDEX_DIR
        self.index_dir = base_dir / f"{self.client.model}_{INDEX_PROMPT_VERSION}"
        self.index_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------ caching ------------------------------ #

    def _doc_cache_key(self, doc_id: str, text: str) -> str:
        content_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
        safe_doc_id = doc_id.replace("/", "_").replace("\\", "_")
        return f"{safe_doc_id}_{content_hash[:16]}"

    def _load_cached_card(self, cache_key: str) -> Optional[Dict[str, Any]]:
        if cache_key in _MEM_CACHE:
            return _MEM_CACHE[cache_key]
        cache_file = self.index_dir / f"{cache_key}.json"
        if cache_file.exists():
            try:
                card = json.loads(cache_file.read_text())
                _MEM_CACHE[cache_key] = card
                return card
            except Exception:
                cache_file.unlink(missing_ok=True)
        return None

    def _store_card(self, cache_key: str, card: Dict[str, Any]) -> None:
        _MEM_CACHE[cache_key] = card
        cache_file = self.index_dir / f"{cache_key}.json"
        cache_file.write_text(json.dumps(card, indent=2))

    # ------------------------------ indexing ----------------------------- #

    def _build_card(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        doc_id = doc["id"]
        title = doc.get("title", doc_id)
        text = doc.get("text") or doc.get("content") or ""

        parsed = self.client.parse(
            prompt=f"Document title: {title}\n\nDocument content:\n{text}",
            schema=DocIndexCard,
            system=INDEXING_SYSTEM_PROMPT,
        )
        return {
            "id": doc_id,
            "title": title,
            "summary": parsed.summary,
            "faqs": parsed.faqs,
            "keywords": parsed.keywords,
        }

    def process(
        self, documents: List[Dict[str, Any]], state: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        cards: Dict[str, Dict[str, Any]] = {}
        to_index: List[Dict[str, Any]] = []

        for doc in documents:
            text = doc.get("text") or doc.get("content") or ""
            cache_key = self._doc_cache_key(doc["id"], text)
            cached = self._load_cached_card(cache_key)
            if cached is not None:
                cards[doc["id"]] = cached
            else:
                to_index.append(doc)

        if to_index:
            print(
                f"🔄 LLM-indexing {len(to_index)} new/changed docs with "
                f"{self.client.model} ({len(cards)} cached) -> {self.index_dir}"
            )

            def index_doc(doc):
                try:
                    return doc, self._build_card(doc)
                except Exception as e:
                    print(f"⚠️  LLM indexing failed for {doc['id']}: {e}")
                    # Degrade gracefully: title-only card, NOT cached, so a
                    # later run retries the LLM call.
                    return doc, {
                        "id": doc["id"],
                        "title": doc.get("title", doc["id"]),
                        "summary": "(indexing failed — content available via read)",
                        "faqs": [],
                        "keywords": [],
                        "_failed": True,
                    }

            with ThreadPoolExecutor(max_workers=self.max_concurrency) as ex:
                for doc, card in ex.map(index_doc, to_index):
                    cards[doc["id"]] = card
                    if not card.get("_failed"):
                        text = doc.get("text") or doc.get("content") or ""
                        self._store_card(self._doc_cache_key(doc["id"], text), card)

        # Preserve the pipeline's document order in the index.
        state[self.state_key] = [cards[doc["id"]] for doc in documents]
        return documents
