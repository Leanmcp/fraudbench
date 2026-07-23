"""Retrieval capability MixIns for the banking_knowledge domain.

Each MixIn provides a single retrieval tool via @is_tool methods.
The ToolKitType metaclass collects these tools through MRO when
mixed into a concrete toolkit class.

MixIns define no __init__ — they expect the concrete class to set
the required attributes (e.g., _kb_pipeline, _grep_pipeline, _sandbox)
during its own __init__.
"""

from tau2.environment.toolkit import ToolKitType, ToolType, is_tool


def _format_kb_search_result(pipeline, retrieval_result) -> str:
    """Format timed KB search results for all KB search tool variants."""
    results = retrieval_result.results
    timing = retrieval_result.timing

    if not results:
        output = "No relevant documents found."
        output += f"\n\n[Timing: retrieval={timing.retrieval_ms:.0f}ms"
        if timing.postprocessing_ms > 0:
            output += f", reranking={timing.postprocessing_ms:.0f}ms"
        output += f", total={timing.total_ms:.0f}ms]"
        return output

    formatted = []
    for i, (doc_id, score) in enumerate(results, 1):
        title = pipeline.get_document_title(doc_id) or "Untitled"
        content = pipeline.get_document_content(doc_id) or ""
        formatted.append(
            f"{i}. {title}\n"
            f"   ID: {doc_id}\n"
            f"   Score: {score:.4f}\n"
            f"   Content: {content}\n"
        )

    output = "\n".join(formatted)
    output += f"\n\n[Timing: retrieval={timing.retrieval_ms:.0f}ms"
    if timing.postprocessing_ms > 0:
        output += f", reranking={timing.postprocessing_ms:.0f}ms"
    output += f", total={timing.total_ms:.0f}ms]"

    if pipeline.state.get("oracle_retriever_active"):
        call_count = pipeline.state.get("oracle_call_count", 1)
        if call_count == 1:
            output += (
                "\n\n[ORACLE RETRIEVAL: The query was ignored. These are the exact "
                "required documents for this task — every KB_search call returns this "
                "identical set regardless of your search terms. One call is sufficient; "
                "do not call KB_search again.]"
            )
        else:
            output += "\n\n[ORACLE RETRIEVAL: The KB has already provided everything you need — no further searches required.]"

    return output


def _run_kb_search(pipeline, query: str, top_k: int | None = None) -> str:
    """Run a KB search pipeline with timing and shared formatting."""
    retrieve_kwargs = {"return_timing": True}
    if top_k is not None:
        retrieve_kwargs["top_k"] = top_k
    retrieval_result = pipeline.retrieve(query, **retrieve_kwargs)
    return _format_kb_search_result(pipeline, retrieval_result)


class KBSearchMixin(metaclass=ToolKitType):
    """MixIn that provides the KB_search tool.

    Expects ``self._kb_pipeline`` (a RetrievalPipeline) to be set by the
    concrete class before any tool calls.
    """

    @is_tool(ToolType.READ)
    def KB_search(self, query: str) -> str:
        """Search the knowledge base for relevant documents.

        Args:
            query: The search query to find relevant documents

        Returns:
            Relevant document excerpts matching the query
        """
        # TODO: clean up knowledge retrieval pipelines to return structure results
        return _run_kb_search(self._kb_pipeline, query)


class GrepMixin(metaclass=ToolKitType):
    """MixIn that provides the grep tool.

    Expects ``self._grep_pipeline`` (a RetrievalPipeline) to be set.
    """

    @is_tool(ToolType.READ)
    def grep(self, pattern: str) -> str:
        """Search for a regex pattern in all knowledge base documents.

        Returns documents ranked by number of matches, with full content.

        Args:
            pattern: The regex pattern to search for (e.g., 'credit.*card', 'fee|charge')

        Returns:
            Matching documents ranked by relevance (match count)
        """
        results = self._grep_pipeline.retrieve(pattern)

        if not results:
            return f"No matches found for pattern: {pattern}"

        formatted = []
        for i, (doc_id, score) in enumerate(results, 1):
            title = self._grep_pipeline.get_document_title(doc_id) or "Untitled"
            content = self._grep_pipeline.get_document_content(doc_id) or ""
            formatted.append(
                f"{i}. {title}\n"
                f"   ID: {doc_id}\n"
                f"   Score: {score:.4f}\n"
                f"   Content: {content}\n"
            )

        return "\n".join(formatted)


class KBSearchBm25AllToolsMixin(metaclass=ToolKitType):
    """BM25 search for AllTools; expects ``self._kb_bm25_pipeline``."""

    @is_tool(ToolType.READ)
    def KB_search_bm25(self, query: str, k: int = 10) -> str:
        """Search the knowledge base using BM25 sparse retrieval.

        Args:
            query: The search query to find relevant documents.
            k: Maximum number of documents to return (default 10).

        Returns:
            Relevant document excerpts matching the query.
        """
        return _run_kb_search(self._kb_bm25_pipeline, query, top_k=k)


class KBSearchDenseAllToolsMixin(metaclass=ToolKitType):
    """Dense embedding search for AllTools; expects ``self._kb_dense_pipeline``."""

    @is_tool(ToolType.READ)
    def KB_search_dense(self, query: str, k: int = 10) -> str:
        """Search the knowledge base using dense embedding retrieval.

        Args:
            query: The search query to find relevant documents.
            k: Maximum number of documents to return (default 10).

        Returns:
            Relevant document excerpts matching the query.
        """
        return _run_kb_search(self._kb_dense_pipeline, query, top_k=k)


class ShellMixin(metaclass=ToolKitType):
    """MixIn that provides the shell tool.

    Expects ``self._sandbox`` (a SandboxManager) to be set.
    """

    @is_tool(ToolType.READ)
    def shell(self, command: str) -> str:
        """Execute a shell command in the knowledge base directory.

        Use standard Unix utilities to explore and search the knowledge base files.
        Common commands: ls, cat, grep, head, tail, find, wc, awk, sed, etc.

        Args:
            command: The shell command to execute (e.g., "ls -la", "grep -r 'credit card' .", "cat INDEX.txt")

        Returns:
            The command output (stdout) or error message
        """
        if self._sandbox is None:
            return "Error: Sandbox not initialized"

        ret_code, stdout, stderr = self._sandbox.run_command(command)

        if ret_code != 0:
            # grep returns 1 when no matches (not an error)
            if ret_code == 1 and "grep" in command and not stderr:
                return "No matches found."
            if stderr:
                return f"Error (exit code {ret_code}): {stderr}"
            return f"Command failed with exit code {ret_code}"

        return stdout if stdout else "(no output)"


class KBSearchLLMMixin(metaclass=ToolKitType):
    """KB_search backed by the LLM-agentic pipeline.

    Same tool name/signature as KBSearchMixin, but the output is the
    retriever LLM's synthesized answer plus source cards, instead of raw
    full-document dumps. Expects ``self._kb_pipeline`` whose retriever is
    ``llm_agentic`` (stashes its answer in state["llm_kb_answer"]) and whose
    doc preprocessor is ``llm_indexer`` (stashes cards in state["llm_index"]).
    """

    @is_tool(ToolType.READ)
    def KB_search(self, query: str) -> str:
        """Search the knowledge base and get a synthesized answer.

        An internal search assistant reads the knowledge-base index, opens
        the relevant documents, and answers your query concisely (with a
        table when appropriate), citing source document IDs. Use
        KB_read_document to see any cited document in full.

        Args:
            query: The question to answer from the knowledge base

        Returns:
            A concise answer with source document IDs, or a report that
            nothing relevant was found
        """
        retrieval_result = self._kb_pipeline.retrieve(query, return_timing=True)
        results = retrieval_result.results
        timing = retrieval_result.timing
        answer = self._kb_pipeline.state.pop("llm_kb_answer", None)

        cards_by_id = {
            card["id"]: card for card in self._kb_pipeline.state.get("llm_index", [])
        }

        parts = []
        if answer:
            parts.append(answer.strip())
        elif not results:
            parts.append("No relevant documents found in the knowledge base.")

        if results:
            source_lines = ["", "Sources:"]
            for i, (doc_id, score) in enumerate(results, 1):
                title = self._kb_pipeline.get_document_title(doc_id) or doc_id
                summary = cards_by_id.get(doc_id, {}).get("summary", "")
                source_lines.append(
                    f"{i}. [{doc_id}] {title} (relevance {score:.1f})"
                    + (f" — {summary}" if summary else "")
                )
            parts.append("\n".join(source_lines))

        parts.append(f"\n[Timing: total={timing.total_ms:.0f}ms]")
        return "\n".join(parts)

    @is_tool(ToolType.READ)
    def KB_read_document(self, doc_id: str) -> str:
        """Read the full content of one knowledge-base document.

        Use this when a KB_search answer cites a document and you need the
        complete details from it.

        Args:
            doc_id: The document ID (as cited by KB_search, e.g. in [brackets])

        Returns:
            The full document content, or an error if the ID is unknown
        """
        content = self._kb_pipeline.get_document_content(doc_id)
        if content is None:
            return f"Unknown document id: {doc_id!r}. Use the IDs cited by KB_search."
        title = self._kb_pipeline.get_document_title(doc_id) or doc_id
        return f"# {title}\nID: {doc_id}\n\n{content}"


class RewriteContextMixin(metaclass=ToolKitType):
    """MixIn that provides the rewrite_context tool for summarization."""

    @is_tool(ToolType.WRITE, mutates_state=False)
    def rewrite_context(self, new_context: str) -> str:
        """Replace your working context with a new summary or condensed version.

        Use this after searching to summarize findings, extract key points,
        or condense information for easier reference later in the conversation.

        Args:
            new_context: The summarized or rewritten context to store

        Returns:
            The context you provided, for reference
        """
        return f"Context updated:\n\n{new_context}"
