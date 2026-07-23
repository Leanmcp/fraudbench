"""Two-stage LLM retriever over LLM-built index cards.

Stage 1 (route):  the LLM sees ONLY the compact index (id, title, summary,
                  FAQs) and picks which documents to open, with 0-10
                  relevance scores. Cheap: cards only, no full docs.
Stage 2 (answer): the LLM reads the FULL text of the selected documents and
                  writes a concise Markdown answer (table where natural).

Pipeline contract is honored: retrieve() returns [(doc_id, score)] so this
retriever also works with the stock KBSearchMixin. The synthesized answer
is stashed in state[answer_state_key] for the KBSearchLLMMixin tool layer.
"""

import os
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from tau2.knowledge.llm_client import KBLLMClient
from tau2.knowledge.registry import register_retriever
from tau2.knowledge.retrievers.base import BaseRetriever

ROUTER_SYSTEM_PROMPT = """\
You are the routing stage of a bank knowledge-base search engine.
You receive an INDEX of all documents (id, title, 1-2 line summary, FAQs),
followed by a query in a separate message. You have NOT read the full
documents.

Select the documents whose FULL text most likely answers the query.
- Prefer precision: pick the few documents that actually cover the asked
  fees/limits/procedures, not everything vaguely related.
- Score each pick 0-10 for how likely its full text answers the query.
- If NOTHING in the index is relevant, return an empty selection.
- Set answerable_from_index=true only if the summaries/FAQs alone already
  contain the complete, specific answer (exact numbers included).
"""

ANSWER_SYSTEM_PROMPT = """\
You are the answering stage of a bank knowledge-base search engine.
You receive the FULL text of the selected documents, followed by the query
in a separate message. Write the answer a support agent needs, USING ONLY
these documents.

Format:
- Be concise. If the answer involves multiple items/tiers/fees/conditions,
  use a compact Markdown table; otherwise short bullet points or 1-3
  sentences.
- Quote exact numbers, fees, limits, timeframes, and eligibility conditions
  verbatim from the documents.
- Cite the supporting document id in-line like [doc_id] after each fact.
- If the documents do NOT contain the answer, say exactly what is missing —
  never guess.
"""


class SelectedDoc(BaseModel):
    doc_id: str = Field(description="id copied exactly from the index")
    relevance: int = Field(description="0-10 likelihood the doc answers the query")


class DocSelection(BaseModel):
    selected: List[SelectedDoc]
    answerable_from_index: bool = Field(
        description="true only if the index cards alone fully answer the query"
    )
    index_answer: str = Field(
        description="the answer from cards alone if answerable_from_index, else ''"
    )


class SynthesizedAnswer(BaseModel):
    answer_markdown: str
    doc_ids_used: List[str]


@register_retriever("llm_agentic")
class LLMAgenticRetriever(BaseRetriever):
    def __init__(
        self,
        model: Optional[str] = None,
        query_key: str = "query",
        index_state_key: str = "llm_index",
        answer_state_key: str = "llm_kb_answer",
        top_k: int = 5,
        max_docs_to_read: int = 4,
        **kwargs,
    ):
        super().__init__(
            model=model,
            query_key=query_key,
            index_state_key=index_state_key,
            answer_state_key=answer_state_key,
            top_k=top_k,
            max_docs_to_read=max_docs_to_read,
            **kwargs,
        )
        self.client = KBLLMClient(model=model)
        self.query_key = query_key
        self.index_state_key = index_state_key
        self.answer_state_key = answer_state_key
        self.top_k = top_k
        self.max_docs_to_read = max_docs_to_read
        # Memoized formatted-index message, keyed by the cards list object.
        # Provider-side prompt caching needs a byte-identical prefix, so the
        # index string must not be rebuilt (or vary) between calls.
        self._index_msg_cache: Optional[Tuple[int, str]] = None

    # --------------------------- prompt building -------------------------- #

    @staticmethod
    def _format_index(cards: List[Dict[str, Any]]) -> str:
        lines = []
        for card in cards:
            lines.append(f"- id: {card['id']}")
            lines.append(f"  title: {card['title']}")
            lines.append(f"  summary: {card['summary']}")
            if card.get("faqs") and os.getenv("SHOW_FAQS", "false").lower() == "true":
                lines.append(f"  faqs: {'; '.join(card['faqs'])}")
        return "\n".join(lines)

    # ------------------------------ stages -------------------------------- #

    def _index_message(self, cards: List[Dict[str, Any]]) -> str:
        if self._index_msg_cache is not None and self._index_msg_cache[0] == id(cards):
            return self._index_msg_cache[1]
        msg = f"INDEX ({len(cards)} documents):\n{self._format_index(cards)}"
        self._index_msg_cache = (id(cards), msg)
        return msg

    def _route(self, query: str, cards: List[Dict[str, Any]]) -> DocSelection:
        # Static prefix (system + index) first, query LAST in its own message:
        # the provider caches the longest byte-identical message prefix, so
        # the big index is billed at the cached rate on every call after the
        # first — only the tiny query message changes.
        messages = [
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": self._index_message(cards)},
            {"role": "user", "content": f"Query: {query}"},
        ]
        return self.client.parse_messages(messages, DocSelection)

    def _answer(
        self,
        query: str,
        docs: List[Tuple[str, str, str]],  # (doc_id, title, full_text)
    ) -> SynthesizedAnswer:
        # Sort by id so the same doc set always yields a byte-identical
        # prefix, whatever relevance order stage 1 returned them in.
        doc_blocks = "\n\n".join(
            f"<document id={doc_id!r} title={title!r}>\n{text}\n</document>"
            for doc_id, title, text in sorted(docs)
        )
        # Same ordering trick: docs first, query last, so repeated reads of
        # the same doc set across queries share a cached prefix.
        messages = [
            {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
            {"role": "user", "content": f"Selected documents:\n{doc_blocks}"},
            {"role": "user", "content": f"Query: {query}"},
        ]
        return self.client.parse_messages(messages, SynthesizedAnswer)

    # ----------------------------- retrieve ------------------------------- #

    def retrieve(
        self, input_data: Dict[str, Any], state: Dict[str, Any]
    ) -> List[Tuple[str, float]]:
        query = input_data.get(self.query_key)
        state.pop(self.answer_state_key, None)  # never leak a stale answer
        if not query or not query.strip():
            return []

        cards = state.get(self.index_state_key)
        if not cards:
            raise ValueError(
                f"No LLM index in state[{self.index_state_key!r}]. "
                "Add the 'llm_indexer' document_preprocessor to the pipeline."
            )
        valid_ids = {card["id"] for card in cards}

        # ---- Stage 1: route over the compact index ----
        selection = self._route(query, cards)
        picks = [s for s in selection.selected if s.doc_id in valid_ids]
        picks.sort(key=lambda s: s.relevance, reverse=True)
        picks = picks[: self.top_k]

        if not picks:
            if selection.answerable_from_index and selection.index_answer.strip():
                state[self.answer_state_key] = selection.index_answer
            return []

        # ---- Stage 2: read full docs, synthesize the answer ----
        if selection.answerable_from_index and selection.index_answer.strip():
            # Cheap path: the cards already answered it; skip the second call
            # but still return sources so the tool output cites documents.
            state[self.answer_state_key] = selection.index_answer
        else:
            content_map = state.get("doc_content_map", {})
            title_map = state.get("doc_title_map", {})
            to_read = picks[: self.max_docs_to_read]
            docs = [
                (
                    s.doc_id,
                    title_map.get(s.doc_id, s.doc_id),
                    content_map.get(s.doc_id, ""),
                )
                for s in to_read
            ]
            try:
                answer = self._answer(query, docs)
                state[self.answer_state_key] = answer.answer_markdown
                # Docs the answer actually used float to the top.
                used = set(answer.doc_ids_used)
                picks.sort(key=lambda s: (s.doc_id in used, s.relevance), reverse=True)
            except Exception as e:
                # Fall back to plain ranked retrieval (stock KB_search shape).
                print(f"⚠️  LLM answer stage failed, returning ranked docs: {e}")

        return [(s.doc_id, s.relevance / 10.0) for s in picks]
