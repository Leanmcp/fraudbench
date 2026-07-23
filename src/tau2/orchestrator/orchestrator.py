import json
import re
import time
import uuid
from abc import ABC, abstractmethod
from copy import deepcopy
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Generic, Literal, Optional, TypeVar

from loguru import logger

from tau2.agent.base_agent import (
    AgentError,
    HalfDuplexAgent,
    is_valid_agent_history_message,
)
from tau2.agent.llm_agent import LLMSoloAgent
from tau2.data_model.message import (
    AssistantMessage,
    Message,
    MultiToolMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from tau2.data_model.simulation import SimulationRun, TerminationReason
from tau2.data_model.tasks import EnvFunctionCall, InitializationData, Task
from tau2.environment.environment import Environment, EnvironmentInfo
from tau2.orchestrator.checklist_tool import CHECKLIST_TOOL_NAME as _CHECKLIST_TOOL_NAME
from tau2.orchestrator.modes import CommunicationMode
from tau2.user.user_simulator import DummyUser, UserSimulator, UserState
from tau2.user.user_simulator_base import (
    HalfDuplexUser,
    UserError,
    is_valid_user_history_message,
)
from tau2.utils.llm_utils import get_cost
from tau2.utils.utils import format_time, get_now


class Role(str, Enum):
    AGENT = "agent"
    USER = "user"
    ENV = "env"


DEFAULT_FIRST_AGENT_MESSAGE = AssistantMessage(
    role="assistant", content="Hi! How can I help you today?", cost=0.0
)

# Task-agnostic harness nudge, injected verbatim (never task/tool/domain-specific —
# doing so would be indistinguishable from leaking the grading answer key). See
# FAILURE_ANALYSIS/analysis/eval-20260719-102147-...__orchestrator_reminder_scoping.md
# for the failure-mode evidence this targets and why the wording is kept generic.
PENDING_ACTION_REMINDER = (
    "<harness_reminder>Before you continue: is there a write/tool-call that you or "
    "the customer still need to trigger to complete this request, that hasn't "
    "happened yet in this conversation? Or are you repeating the same response "
    "without new progress? If either is true, move toward resolving it now rather "
    "than only continuing to answer questions.</harness_reminder>"
)

# NF6 ("silent-success illusion") countermeasure -- see
# FAILURE_ANALYSIS/analysis/eval-20260719-102147-...__failure_deepdive/06_silent_success_illusion.md.
# That doc's own diagnosis: every failure in a 65-task sample ended user_stop with the
# customer satisfied, because the agent's closing summary restates the value it MEANT
# to write, not one it re-checked against the actual tool results. Its own proposed fix
# lever, quoted directly: "a mandatory self-verification beat before wrapping up --
# re-state the specific numbers/entities just written and reconcile them against the
# customer's original ask, preferring to re-derive from calls already made in the
# session over issuing new discoverable-tool reads." The explicit constraint on NOT
# issuing new discoverable-tool calls is deliberate and load-bearing: that same doc
# flags that a naive "always double-check" fix could itself trip the exact
# extra-discoverable-tool-call anti-target already confirmed on task_046 (see the
# 2026-07-23 update in __orchestrator_reminder_scoping.md) -- re-reading what's already
# in context is safe, calling a tool again purely to verify is not.
SELF_VERIFICATION_REMINDER = (
    "<harness_reminder>Before you state any specific number, amount, category, or "
    "classification to the customer as final: re-derive it from the actual tool "
    "results already visible earlier in this conversation, rather than repeating a "
    "value you computed or decided on without re-checking it against what was "
    "actually returned. Do not call a new tool just to double-check this -- only "
    "re-read what has already been returned to you in this session. If you haven't "
    "stated any such value yet, ignore this.</harness_reminder>"
)

# Two-phase, STRUCTURED templates for the named-workflow-skillset re-check
# (2026-07-22, v3). The name LIST is supplied by the caller (domain-specific, e.g.
# TAU2_WITH_TINKER/tau2_env.py for banking_knowledge's
# PROMPT_LIBRARY/banking_knowledge/workflow_skillsets.md) -- this orchestrator stays
# domain-agnostic and only knows how to format whatever names it's given.
#
# History: v1 re-listed all names and re-asked "which one applies" on EVERY fire.
# v2 split into a one-time CLASSIFY + a "review what you said earlier" REVIEW, but
# REVIEW still just asked the agent to recall its own prior text from context --
# there was no code anywhere that parsed, stored, or verified what the agent wrote;
# "reviewing the list" meant "hoping the model re-reads its own earlier turn."
# v3 makes the checklist a REAL parsed, orchestrator-held data structure
# (self._workflow_checklists, populated by _parse_and_store_workflow_checklist from
# a fenced <workflow_checklist> block in the agent's own reply): REVIEW now echoes
# back the last state the harness itself recorded, not a request that the model
# remember it. This still cannot guarantee the *initial* classification is correct
# -- deciding which skillset applies is a judgment call no parser can verify
# without the golden answer, which would be leakage -- but it does guarantee the
# tracked state is faithful to what was last actually written, not dependent on
# the model's own attention across turns.
# v4 (2026-07-22) adds a detection gate in front of CLASSIFY
# (_matched_workflow_skill_names, driven by the optional
# pending_action_reminder_workflow_triggers constructor arg): before v4, CLASSIFY
# always listed every configured name regardless of what the customer actually
# said, which a 7-task baseline read (see
# FAILURE_ANALYSIS/analysis/eval-20260719-102147-...__stop_signal_detector_scoping.md)
# found would surface irrelevant skill names on unrelated tasks. The gate scans
# only the customer's own words, never tool calls or ledger state, and is a no-op
# (unchanged v3 behavior) when pending_action_reminder_workflow_triggers isn't
# supplied.
WORKFLOW_SKILL_CLASSIFY_TEMPLATE = (
    "<harness_reminder>Before you continue: if resolving this request will take "
    "more than one action or turn, write a concrete checklist for "
    "THIS specific conversation in exactly this format (instantiate it against "
    "what the customer actually asked for, don't just copy generic wording)."
    "{named_hint}\n"
    '<workflow_checklist skill="short name for this specific request">\n'
    "- [ ] first required step, made specific to this conversation\n"
    "- [ ] second required step\n"
    "</workflow_checklist>\n"
    'Use "- [x]" for a step already completed. This harness parses and tracks '
    "this block automatically; you'll see your last recorded state echoed back "
    "later, so you don't need to remember or repeat it yourself from memory. This "
    "is your own private tracking note, not something to explain or justify to "
    "the customer -- write it inside your thinking, before you close your "
    "reasoning and give your actual reply. It is not a substitute for that "
    "reply: after you've noted the checklist in your thinking, still write a "
    "real reply to the customer (or make a tool call) in that same turn, the "
    "same way you would if you hadn't written a checklist at all. If the matched "
    'per card, per account), write one separate <workflow_checklist skill="..."> '
    "block per entity -- name the entity inside that skill attribute (e.g. "
    'skill="lost wallet / multi-card replacement (Blue Account debit card)") '
    "instead of merging them into a single checklist."
    "</harness_reminder>"
)
# Filled into {named_hint} above only when specific named skillsets are configured
# (banking_knowledge's PROMPT_LIBRARY/.../workflow_skillsets.md via the
# "oracle_skillset" retrieval variant) -- gives the model a lightweight index to
# check against before falling back to inventing its own ad hoc plan. Empty string
# when no named skillsets are configured (pure ad hoc "universal" mode): the model
# gets no hint at all about what a "multi-step request" might look like beyond its
# own judgment, same information state as base policy guideline #11 has today.
WORKFLOW_SKILL_NAMED_HINT_TEMPLATE = (
    " Check first whether it matches one of your named workflow skillsets "
    "({names}) and, if so, use that skillset's own step order."
)
WORKFLOW_SKILL_REVIEW_TEMPLATE = (
    "<harness_reminder>Your last recorded workflow checklist:\n{checklist}\n"
    'Update it now (same <workflow_checklist skill="..."> format, written '
    "inside your thinking, not your reply) to reflect what's actually done, "
    "and act on whatever remains -- then still give the customer a real "
    "reply or tool call this turn, same as always."
    "</harness_reminder>"
)
WORKFLOW_SKILL_REVIEW_NONE_TEMPLATE = (
    "<harness_reminder>No workflow checklist has been recorded yet this "
    "conversation. If resolving this request will take more than one action, "
    'write one now using the <workflow_checklist skill="..."> format described '
    "earlier -- checking your named workflow skillsets first if any apply."
    "</harness_reminder>"
)

# --------------------------------------------------------------------------- #
# Plan pinning (2026-07-27) -- the primitive behind the counterfactual ablation
# and the oracle-ceiling experiment. See
# FAILURE_ANALYSIS/analysis/workflow_tool_and_checklist_tool_scoping.md section 3.
#
# Instead of asking the agent to WRITE a checklist and echoing back whatever it
# wrote, the harness HANDS it a fixed checklist and echoes that. Because the
# plan is then an experimental variable rather than a model output, three arms
# become comparable on the same tasks:
#
#   off     the agent writes its own plan (current behavior, the control arm)
#   null    a vacuous one-line plan carrying no information
#   oracle  the canonical steps from workflow_steps.json for the matched skill
#
# R(own) - R(null) is the plan's causal value: if it is ~0 the checklist is
# decorative and the whole mechanism is not worth optimising. R(oracle) - R(own)
# is the headroom: if it is ~0, handing the model a perfect plan does not help,
# so planning is not the bottleneck and routing/tool work is pointless.
#
# Why this rides the existing reminder mechanism rather than a new tool: the
# workflow tools are not built yet, and building them first would mean the
# ablation could only ever measure the new mechanism, never the current one.
# This way the control arm IS the shipped behavior.
PINNED_PLAN_TEMPLATE = (
    "<harness_reminder>Your workflow checklist for this conversation has "
    "already been prepared for you:\n{checklist}\n"
    "Work through it, and act on whatever remains. You do not need to write "
    "your own checklist -- use this one. Give the customer a real reply or "
    "make a tool call this turn, same as always."
    "</harness_reminder>"
)

#: The null arm's plan. Deliberately shaped like a real checklist -- same
#: wrapper, same item syntax, one item -- so the ONLY difference from the
#: oracle arm is informational content, not format or token position. A null
#: arm that also changed the shape of the injection would confound "the plan's
#: content mattered" with "something plan-shaped was present".
NULL_PLAN_SKILL = "this request"
NULL_PLAN_ITEMS = ["Handle the customer's request"]

_HARNESS_SYSTEM_REMINDER_MARKER = "<harness_system_reminder>"
#: Separate slot for the enforcement retry. It MUST NOT share the reminder's
#: marker: _deliver_reminder_as_system_message updates the matching message in
#: place, so sharing one marker meant the retry overwrote the CLASSIFY template
#: -- deleting the format example the retry then told the model to follow.
_HARNESS_ENFORCEMENT_MARKER = "<harness_enforcement_reminder>"

# The format spec is REPEATED here in full, deliberately. The previous version
# said "in the exact format already described" and relied on the CLASSIFY
# template still being in context -- but both are delivered through
# _deliver_reminder_as_system_message, which overwrites the single marked
# SystemMessage in place, so the retry DESTROYED the example it was pointing at.
# Confirmed from the model's own reasoning on 2026-07-27: "The format: a
# JSON-like block? ... The example is not given exactly, but we need to produce
# something that matches the schema". It was guessing, and its guesses (bare tag,
# numbered items) were then discarded by the parser.
CHECKLIST_ENFORCEMENT_NUDGE = (
    "<harness_reminder>Your previous attempt at this turn did not include a "
    "usable <workflow_checklist> block. This is not optional this time. Write it "
    "now, inside your thinking, before anything else, in EXACTLY this format:\n"
    '<workflow_checklist skill="short name for this specific request">\n'
    "- [ ] first required step, made specific to this conversation\n"
    "- [ ] second required step\n"
    "</workflow_checklist>\n"
    'Use "- [x]" for a step already completed. Then give your reply or tool '
    "call as normal.</harness_reminder>"
)

# --------------------------------------------------------------------------- #
# Tool-mode templates (2026-07-27). Same three moments as above -- classify,
# review, enforce -- but pointing at the `update_checklist` tool instead of a
# text block. See src/tau2/orchestrator/checklist_tool.py for why.
#
# These are deliberately SHORTER than their text-mode counterparts. Most of the
# text-mode wording is format specification ("exactly this format", "use - [x]",
# "one block per entity"), and the tool's own schema carries all of that. What
# is left is when to call it and the reminder that calling it does not excuse
# the agent from actually replying -- the one instruction the schema cannot
# express. That length difference is a real confound for a tool-vs-text
# comparison and is called out in the runner script.
WORKFLOW_SKILL_CLASSIFY_TOOL_TEMPLATE = (
    "<harness_reminder>Before you continue: if resolving this request will take "
    "more than one action or turn, call the `update_checklist` tool now with the "
    "concrete steps for THIS specific conversation (name the actual accounts, "
    "cards and amounts involved -- don't restate a generic procedure).{named_hint}\n"
    "This harness tracks that checklist for you and echoes your last recorded "
    "state back later, so you don't need to remember it yourself. It is a "
    "private tracking note, not something to explain to the customer. Calling it "
    "is not a substitute for handling the request: continue with the customer "
    "reply or the real tool call in the normal way."
    "</harness_reminder>"
)
WORKFLOW_SKILL_REVIEW_TOOL_TEMPLATE = (
    "<harness_reminder>Your last recorded workflow checklist:\n{checklist}\n"
    "If anything has changed or been completed, call `update_checklist` again "
    "with the full updated step list. Then act on whatever remains -- and still "
    "give the customer a real reply or tool call this turn, same as always."
    "</harness_reminder>"
)
# The "once" variant. Two wording changes from the every-turn template, both
# forced by the mode rather than cosmetic:
#   * it does NOT promise the state will be echoed back later, because in this
#     mode it will not be. Promising an echo that never arrives would teach the
#     model its plan was lost.
#   * it says explicitly that this is the only time it will be asked, and that
#     calling the tool later is its own call. In every_turn mode the harness
#     decides; here the model does, and it can only act on that if it is told.
WORKFLOW_SKILL_CLASSIFY_TOOL_ONCE_TEMPLATE = (
    "<harness_reminder>You have an `update_checklist` tool available. If "
    "resolving this request will take more than one action or turn, calling it "
    "with the concrete steps for THIS conversation will help you keep track "
    "(name the actual accounts, cards and amounts involved).{named_hint}\n"
    "This is the only time you'll be reminded -- from here on, whether and when "
    "to call it is your judgment, including calling it again as the plan "
    "changes. Either way, handle the customer's request as normal this turn."
    "</harness_reminder>"
)
WORKFLOW_SKILL_REVIEW_NONE_TOOL_TEMPLATE = (
    "<harness_reminder>No workflow checklist has been recorded yet this "
    "conversation. If resolving this request will take more than one action, "
    "call `update_checklist` now -- checking your named workflow skillsets "
    "first if any apply.</harness_reminder>"
)
CHECKLIST_ENFORCEMENT_NUDGE_TOOL = (
    "<harness_reminder>Your previous attempt at this turn did not call "
    "`update_checklist`. This is not optional this time: call it now with the "
    "steps for this specific request. Then continue as normal."
    "</harness_reminder>"
)

# Loosened 2026-07-27. Both patterns used to be strict, and BOTH had to match for
# a checklist to be recorded, so a block that got either detail wrong was thrown
# away whole. Measured over one 10-episode control arm: the model wrote blocks in
# 5 episodes, and exactly 1 survived. Tasks 028/050/052 produced 109 perfectly
# good steps between them, numbered "1." instead of "- [ ]", and every one was
# discarded; task_073 wrote 18 blocks with no skill attribute, likewise
# discarded. The harness then "enforced" 147 retries against episodes that had
# already complied.
#
#   skill attribute -- now OPTIONAL. Absent means the checklist is unnamed, which
#                      is fine: the name is a label, not the content.
#   items           -- now accepts "- [ ] x", "- [x] x", "1. x", "1) x", "- x".
#                      Only the checkbox forms can express done-ness; the others
#                      are recorded as not-done, which is correct for a plan the
#                      model just wrote.
_WORKFLOW_CHECKLIST_BLOCK_RE = re.compile(
    r'<workflow_checklist(?:\s+skill="([^"]*)")?[^>]*>(.*?)</workflow_checklist>',
    re.DOTALL,
)
#: group(1) = "x"/" " when a checkbox was used, else None; group(2) = the text.
_WORKFLOW_CHECKLIST_ITEM_RE = re.compile(
    r"^\s*(?:[-*]\s*\[([ xX])\]|\d+[.)]|[-*])\s+(.+?)\s*$"
)

# Type variables for generic orchestrators
# Base types for BaseOrchestrator - unbound to allow both half-duplex and full-duplex
BaseAgentT = TypeVar("BaseAgentT")
BaseUserT = TypeVar("BaseUserT")
TrajectoryItemT = TypeVar(
    "TrajectoryItemT"
)  # Message for half-duplex, Tick for full-duplex

# Half-duplex specific types for Orchestrator
AgentT = TypeVar("AgentT", bound=HalfDuplexAgent)
UserT = TypeVar("UserT", bound=HalfDuplexUser)


class BaseOrchestrator(ABC, Generic[BaseAgentT, BaseUserT, TrajectoryItemT]):
    """
    Abstract base class for orchestrators.

    Provides the common infrastructure for managing simulations between Agent, User,
    and Environment. Subclasses implement specific communication patterns:
    - Orchestrator: Half-duplex (turn-based) communication, trajectory of Messages
    - FullDuplexOrchestrator: Full-duplex (streaming) communication, trajectory of Ticks

    Type Parameters:
        BaseAgentT: The agent type
        BaseUserT: The user type
        TrajectoryItemT: The trajectory item type (Message for half-duplex, Tick for full-duplex)

    Shared Responsibilities:
        - Environment initialization and tool execution
        - Termination tracking (max steps, max errors, done state)
        - Trajectory management
        - Simulation run lifecycle (initialize, step loop, finalize)

    Subclass Responsibilities:
        - Communication-specific initialization
        - Step implementation for their communication pattern
        - Mode-specific termination checks
    """

    def __init__(
        self,
        domain: str,
        agent: BaseAgentT,
        user: BaseUserT,
        environment: Environment,
        task: Task,
        max_steps: int = 100,
        max_errors: int = 10,
        seed: Optional[int] = None,
        simulation_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        """
        Initialize the base orchestrator.

        Args:
            domain: The domain name of the simulation (e.g., 'airline', 'retail', 'telecom').
            agent: The agent instance.
            user: The user instance.
            environment: The environment instance that handles tool execution.
            task: The task specification containing initial state, goals, and evaluation criteria.
            max_steps: Maximum number of simulation steps before termination. Defaults to 100.
            max_errors: Maximum number of tool execution errors before termination. Defaults to 10.
            seed: Optional random seed for reproducibility. Defaults to None.
            simulation_id: Optional simulation ID. Defaults to generated UUID.
            timeout: Maximum wallclock time in seconds. None means no timeout.
        """
        self.domain = domain
        self.agent: BaseAgentT = agent
        self.user: BaseUserT = user
        self.environment = environment
        self.task = task
        self.seed = seed
        self.simulation_id = simulation_id or str(uuid.uuid4())

        # State tracking
        self.agent_state: Optional[Any] = None
        self.user_state: Optional[UserState] = None

        # Termination tracking
        self.max_steps: int = max_steps
        self.max_errors: int = max_errors
        self.timeout: Optional[float] = timeout
        self.step_count: int = 0
        self.done: bool = False
        self.termination_reason: Optional[TerminationReason] = None
        self.num_errors: int = 0
        self._run_start_time: Optional[str] = None
        self._run_start_perf: Optional[float] = None

    @abstractmethod
    def initialize(self) -> None:
        """
        Initialize the orchestrator for simulation.

        Subclasses must implement mode-specific initialization:
        - Set up environment state
        - Initialize agent and user states
        - Set up initial messages/chunks
        """
        pass

    @abstractmethod
    def step(self) -> None:
        """
        Perform one step of the simulation.

        Subclasses implement their communication pattern:
        - Half-duplex: Turn-based message passing
        - Full-duplex: Simultaneous chunk generation
        """
        pass

    @abstractmethod
    def get_trajectory(self) -> list[TrajectoryItemT]:
        """
        Get the trajectory of the simulation.

        Returns:
            List of trajectory items. Type depends on orchestrator mode:
            - Orchestrator (half-duplex): list[Message]
            - FullDuplexOrchestrator: list[Tick]
        """
        pass

    @abstractmethod
    def get_messages(self) -> list[Message]:
        """
        Get all messages from the simulation as a flat list.

        This provides a consistent way to get messages regardless of orchestrator mode.
        For half-duplex, this is the same as get_trajectory().
        For full-duplex, this returns linearized messages from all ticks.

        Returns:
            List of all messages sorted by timestamp with turn_idx assigned.
        """
        pass

    @abstractmethod
    def _validate_mode_compatibility(self) -> None:
        """
        Validate that the agent and user support this communication mode.

        Raises:
            ValueError: If agent or user don't support the required mode.
        """
        pass

    @abstractmethod
    def _check_termination(self) -> None:
        """
        Check for termination conditions specific to this communication mode.

        Sets self.done and self.termination_reason if termination conditions are met.
        """
        pass

    @abstractmethod
    def _finalize(self) -> SimulationRun:
        """
        Finalize the simulation and create the SimulationRun result.

        Called after the simulation loop completes. Should:
        - Send stop signals to agent and user
        - Calculate costs
        - Build and return SimulationRun

        Returns:
            SimulationRun with all simulation data.
        """
        pass

    def _check_timeout(self) -> None:
        if (
            self.timeout is not None
            and self._run_start_perf is not None
            and not self.done
        ):
            elapsed = time.perf_counter() - self._run_start_perf
            if elapsed >= self.timeout:
                self.done = True
                self.termination_reason = TerminationReason.TIMEOUT
                logger.info(
                    f"Simulation timed out after {elapsed:.1f}s (timeout={self.timeout}s)"
                )

    def _cleanup(self) -> None:
        """Best-effort cleanup of agent and user resources.

        Called from the ``finally`` block of :meth:`run` so that WebSocket
        connections, background threads, and other resources are released
        even when ``step()`` raises an unexpected exception.

        On the normal (non-error) path ``_finalize()`` handles cleanup
        as part of building the result, so this method is a no-op.
        """
        try:
            if hasattr(self, "agent") and self.agent is not None:
                self.agent.stop(None, getattr(self, "agent_state", None))
        except Exception as e:
            logger.warning(f"Error during agent cleanup: {e}")

        try:
            if hasattr(self, "user") and self.user is not None:
                self.user.stop(None, getattr(self, "user_state", None))
        except Exception as e:
            logger.warning(f"Error during user cleanup: {e}")

    def run(self) -> SimulationRun:
        """
        Run the simulation.

        Template method that orchestrates the simulation lifecycle:
        1. Initialize the simulation
        2. Step until done
        3. Check termination conditions after each step
        4. Finalize and return results

        Returns:
            SimulationRun: The simulation run with all results.
        """
        self._run_start_time = get_now()
        self._run_start_perf = time.perf_counter()
        self.initialize()

        finalized = False
        try:
            while not self.done:
                self.step()
                self._check_termination()
            result = self._finalize()
            finalized = True
            return result
        finally:
            if not finalized:
                logger.warning(
                    "Simulation loop exited with an exception — "
                    "running emergency cleanup"
                )
                self._cleanup()

    def _initialize_environment(
        self,
        initialization_data: Optional[InitializationData],
        initialization_actions: Optional[list[EnvFunctionCall]],
        message_history: list[Message],
    ) -> None:
        """
        Initialize the environment with the given state.

        Args:
            initialization_data: Optional data to initialize environment state.
            initialization_actions: Optional actions to execute during initialization.
            message_history: Message history for context.
        """
        self.environment.set_state(
            initialization_data=initialization_data,
            initialization_actions=initialization_actions,
            message_history=message_history,
        )

    def _execute_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolMessage]:
        """
        Execute tool calls and return results.

        Args:
            tool_calls: List of tool calls to execute.

        Returns:
            List of ToolMessage results from the environment.
        """
        tool_results = []
        for tool_call in tool_calls:
            # The checklist tool is a HARNESS tool, not an environment one: it
            # touches no DB, mutates no state the grader reads, and must never
            # reach environment.get_response (which would return an unknown-tool
            # error and count against max_errors). Its state was already
            # recorded in _parse_and_store_checklist_tool_calls when the turn
            # was produced; all that is left here is the acknowledgement.
            # `requestor == "assistant"` is part of the condition, not an
            # assumption. The checklist tool is offered to the AGENT only, but
            # nothing stops the user simulator from emitting a call by that name
            # -- tool-call parsing recovers whatever `to=functions.<name>` the
            # model wrote and does not check it against that speaker's schema
            # list. That happened on 2026-07-28: the user sim produced an
            # `update_checklist` call, this branch answered it with a
            # hard-coded requestor="assistant", the orchestrator routed the ack
            # back to the USER (tool results return to whoever called), and
            # UserSimulator.flip_roles refused it -- "Tool messages should be
            # sent to the user in this message history" -- killing the episode.
            # Falling through to the environment instead yields an unknown-tool
            # error for the user, which is the correct answer: the user does not
            # have this tool.
            if (
                self.checklist_tool
                and tool_call.name == _CHECKLIST_TOOL_NAME
                and getattr(tool_call, "requestor", "assistant") == "assistant"
            ):
                from tau2.orchestrator.checklist_tool import (
                    checklist_ack,
                    parse_checklist_call,
                )

                self._checklist_tool_calls += 1
                parsed = parse_checklist_call(tool_call.name, tool_call.arguments)
                tool_results.append(
                    ToolMessage(
                        id=tool_call.id,
                        role="tool",
                        content=checklist_ack(parsed),
                        requestor="assistant",
                        error=False,
                    )
                )
                continue
            tool_result = self.environment.get_response(tool_call)
            if tool_result.error:
                self.num_errors += 1
            tool_results.append(tool_result)
        return tool_results

    def _wrap_tool_results(self, tool_results: list[ToolMessage]) -> Message:
        """
        Wrap tool results in appropriate message type.

        Args:
            tool_results: List of tool message results.

        Returns:
            Single ToolMessage if one result, MultiToolMessage if multiple.
        """
        if len(tool_results) > 1:
            return MultiToolMessage(role="tool", tool_messages=tool_results)
        return tool_results[0]

    def _get_environment_info(self) -> EnvironmentInfo:
        """Get the environment info."""
        return self.environment.get_info()


class Orchestrator(BaseOrchestrator[AgentT, UserT, Message]):
    """
    Orchestrator for half-duplex (turn-based) simulation.

    Passes messages between the Agent, User, and Environment in alternating turns.

    Communication Protocol:
        The orchestrator manages message flow between three roles: AGENT, USER, and ENV(ironment).
        Messages are passed in a turn-based manner following these rules:

        Message Types:
            - AssistantMessage: Sent by the agent
            - UserMessage: Sent by the user
            - ToolMessage: Sent by the environment in response to tool calls
            - MultiToolMessage: Wraps multiple tool messages when multiple tool calls are made

        Message Content Rules:
            1. Messages must contain EITHER text content OR tool calls, never both
            2. Messages cannot be empty (must have either text or tool calls)
            3. Tool calls must be followed by corresponding tool messages from the environment

        Communication Flow:
            - AGENT -> USER: Agent sends text response to user
            - AGENT -> ENV: Agent makes tool call(s) to environment
            - USER -> AGENT: User sends text message to agent
            - USER -> ENV: User makes tool call(s) to environment
            - ENV -> AGENT: Environment returns tool results to agent (after agent's tool call)
            - ENV -> USER: Environment returns tool results to user (after user's tool call)

        Solo Mode:
            In solo mode, the user is replaced by a DummyUser and the agent operates autonomously:
            - Agent can ONLY send tool calls (no text messages to user)
            - Exception: Agent can send stop signal (###STOP###) to end simulation
            - Agent interacts exclusively with the environment until completion

        Termination:
            Simulation ends when:
            - Agent sends stop signal (###STOP###)
            - User sends stop signal
            - Maximum steps (max_steps) reached
            - Maximum errors (max_errors) reached
            - Communication protocol violation detected (if validate_communication=True)
    """

    def __init__(
        self,
        domain: str,
        agent: AgentT,
        user: UserT,
        environment: Environment,
        task: Task,
        max_steps: int = 100,
        max_errors: int = 10,
        seed: Optional[int] = None,
        solo_mode: bool = False,
        simulation_id: Optional[str] = None,
        validate_communication: bool = False,
        timeout: Optional[float] = None,
        pending_action_reminder_interval: Optional[int] = None,
        pending_action_reminder_start: int = 0,
        pending_action_reminder_use_ledger_state: bool = False,
        pending_action_reminder_workflow_names: Optional[list[str]] = None,
        pinned_plan: Optional[str] = None,
        pinned_plan_steps: Optional[dict] = None,
        checklist_tool: bool = False,
        checklist_prompt: Literal["every_turn", "once"] = "every_turn",
        checklist_enforce: bool = True,
        planner_model: Optional[str] = None,
        planner_domain_policy: Optional[str] = None,
        planner_llm_args: Optional[dict] = None,
        pending_action_reminder_workflow_triggers: Optional[
            dict[str, list[list[str]]]
        ] = None,
        pending_action_reminder_delivery: Literal["message", "system"] = "message",
        pending_action_reminder_checklist_scope: Literal[
            "named_only", "universal"
        ] = "named_only",
        self_verification_reminder: bool = False,
    ):
        """
        Initialize the Orchestrator for managing simulation between Agent, User, and Environment.

        This orchestrator implements half-duplex (turn-based) communication where agent and user
        alternate sending complete messages. For streaming/full-duplex communication, use
        FullDuplexOrchestrator instead.

        Args:
            domain: The domain name of the simulation (e.g., 'airline', 'retail', 'telecom').
            agent: The agent instance that will respond to user requests and make tool calls.
            user: The user instance that interacts with the agent (can be UserSimulator or DummyUser).
            environment: The environment instance that handles tool execution and maintains state.
            task: The task specification containing initial state, goals, and evaluation criteria.
            max_steps: Maximum number of simulation steps before termination. Defaults to 100.
            max_errors: Maximum number of tool execution errors before termination. Defaults to 10.
            seed: Optional random seed for reproducibility of agent and user behavior. Defaults to None.
            solo_mode: If True, agent operates without user interaction (only tool calls allowed).
                      Requires agent to be LLMSoloAgent or GymAgent, and user to be DummyUser.
                      Defaults to False.
            validate_communication: If True, validates communication protocol rules (e.g., no mixed
                                   messages with both text and tool calls). Defaults to False.
            timeout: Maximum wallclock time in seconds. None means no timeout.
            pending_action_reminder_interval: If set, injects a fixed, task-agnostic
                "is there a pending action?" nudge onto the message the agent is about
                to see, every N times the agent is about to take a turn. None (default)
                disables the nudge entirely, so existing eval runs are unaffected unless
                this is explicitly opted into. See
                FAILURE_ANALYSIS/analysis/eval-20260719-102147-...__orchestrator_reminder_scoping.md.
            pending_action_reminder_start: How many agent turns to let pass before the
                nudge can first fire (0 = eligible from the agent's very first turn).
                Only used when pending_action_reminder_interval is set. Empirical data in
                the scoping doc above shows the decisive mistake in short STALL-shaped
                episodes is often locked in within the first few agent turns, so a large
                warm-up defeats the purpose for that failure family.
            pending_action_reminder_use_ledger_state: If True, and the environment's
                toolkit exposes get_agent_discoverable_tools_state()/
                get_user_discoverable_tools_state() (currently banking_knowledge
                only), appends a second, mechanically-derived line naming any
                discoverable tool that has been unlocked/given but never actually
                called yet in this session -- computed purely from this session's
                own tool-call record, never from golden/expected actions, so it
                carries the same no-leakage guarantee as the generic reminder.
                Falls back to the generic reminder alone when the environment
                doesn't expose the ledger, or nothing is currently pending.
                Defaults to False (generic reminder only).
            pending_action_reminder_workflow_names: If set, appends a third,
                two-phase line: the FIRST time it fires in an episode, it lists
                these workflow-skillset names and asks the agent to classify the
                conversation against them and write a concrete, conversation-
                specific checklist; every later firing instead asks the agent to
                review the checklist it already established, without re-listing
                the names (v2, 2026-07-22 -- v1 re-listed all names on every
                firing, which read as repetitive and didn't match a real todo-list
                habit; changed after reviewing a real pilot transcript). The
                orchestrator does not know what these names mean or carry their
                step content -- that lives in domain-specific prompt content
                (e.g. PROMPT_LIBRARY/banking_knowledge/workflow_skillsets.md, paired
                with the "oracle_skillset" retrieval variant). See the doc cited
                above for why this is expected to behave differently from the
                already-tested static
                "oracle_checklist" variant. None (default) omits this line entirely.
            pending_action_reminder_workflow_triggers: Optional detection gate for
                pending_action_reminder_workflow_names (2026-07-22). Maps a skill
                name from that list to a list of keyword-groups: `[["a"], ["b",
                "c"]]` means "match if the customer's own words (across the whole
                conversation so far) contain 'a', OR contain both 'b' and 'c'".
                Purely mechanical substring matching, scanned ONLY over
                UserMessage content (never tool-call results, ledger state, or
                agent text) so it can't misfire on tools shared across skillsets.
                Re-scanned fresh on every reminder firing, not cached from turn 0,
                so a second request that surfaces mid-conversation still gets
                picked up. Any name in pending_action_reminder_workflow_names with
                NO entry in this dict (e.g. "identity verification", whose own
                Triggers-on condition is near-universal) is always treated as
                matched -- this dict narrows which *situational* skills get
                surfaced, it does not gate the ones that were never meant to be
                situational. When this is None (default), behavior is unchanged
                from before this parameter existed: every configured name is
                always surfaced, ungated. The orchestrator does not interpret the
                keyword strings beyond substring containment -- the actual
                trigger words live in domain-specific caller code (e.g.
                eval_tau2_tinker.py's PENDING_ACTION_REMINDER_WORKFLOW_TRIGGERS,
                sourced verbatim from each skillset's own "Triggers on" line in
                PROMPT_LIBRARY/banking_knowledge/workflow_skillsets.md -- already-
                shipped policy text, not new leakage). See
                FAILURE_ANALYSIS/analysis/eval-20260719-102147-...
                __stop_signal_detector_scoping.md for the evidence this was built
                against: a 7-task baseline read where the un-gated classify
                template would have surfaced skill names the customer never
                mentioned.
            pending_action_reminder_delivery: How the reminder text reaches the
                agent. "message" (default): appended as trailing text onto the
                UserMessage/ToolMessage the agent is about to see -- cheap, works
                for any agent implementation, but untested whether a model weighs
                content embedded in a user/tool turn as strongly as a directive in
                the system prompt (where base policy guidelines like #8-11 live).
                "system": instead inserts/updates a dedicated SystemMessage in the
                agent's own state.system_messages (duck-typed on that attribute --
                LLMAgent's LLMAgentState exposes it; agents that don't expose it
                silently fall back to "message" for that turn). This isolates
                role (system vs user/tool) from the rest of the mechanism, at the
                cost of the reminder always sitting at the same early position
                (right after the domain policy) rather than freshly at the most
                recent turn -- it does not by itself test recency, only role.
                Not yet compared against "message" on any task as of 2026-07-22.
            pending_action_reminder_checklist_scope: "named_only" (default,
                unchanged behavior): the structured, harness-tracked
                <workflow_checklist> mechanism only engages when
                pending_action_reminder_workflow_names is set; otherwise the
                agent only gets the bare PENDING_ACTION_REMINDER line with no
                tracked state. "universal": the checklist mechanism (build once,
                harness echoes back the last recorded state on every later
                firing) engages for ANY multi-step request, whether or not it
                matches a named skillset -- when no names are configured, or the
                model's own checklist doesn't match any of them, it builds an ad
                hoc checklist from its own judgment instead. The bare
                PENDING_ACTION_REMINDER line is dropped in this mode (the
                checklist ask subsumes its intent) to avoid duplicating text.
                Rationale: the generic reminder was found to add no measurable
                value over base-policy guideline #11 on a same-day comparison
                (`010`/`016`, 2026-07-22) -- one live hypothesis is that this is
                because guideline #11 already asks for the same self-checklist
                habit and the untracked reminder had nothing to add, whereas the
                *tracked* version (harness-stored state, verbatim echo-back
                instead of relying on the model's own memory of its earlier
                turns) might not be redundant with a purely voluntary
                instruction. Untested as of 2026-07-22 -- this mode exists to
                test that hypothesis, not because it's confirmed to help. Also
                carries a real, not yet measured risk: extending the checklist
                habit to previously-unaffected simple tasks could push the same
                extra-call anti-target failure (`044`/`046`) into a wider set of
                tasks than before.
            self_verification_reminder: If True, appends SELF_VERIFICATION_REMINDER
                to the pending-action-reminder firing (2026-07-23; see
                FAILURE_ANALYSIS/.../failure_deepdive/06_silent_success_illusion.md
                -- "NF6", the finding that every failure in a 65-task sample ended
                user_stop with the customer satisfied because the agent's closing
                summary restated an intended value rather than one re-checked
                against actual tool results). Gated on `_any_tool_call_made()`
                returning True -- fires only once the agent has actually called at
                least one tool this session, since there's nothing to verify
                before that. Deliberately does NOT tell the model to call any new
                tool to double-check; that doc's own honest-assessment section
                flags a naive "always double-check" fix as capable of tripping the
                same extra-discoverable-tool-call anti-target already confirmed on
                `046` (see the 2026-07-23 update in
                __orchestrator_reminder_scoping.md) -- the reminder text
                explicitly says to re-derive only from what's already in context.
                Default False (off); only used when pending_action_reminder_interval
                is also set, since it fires on the same per-turn schedule.
        """
        # Initialize base class
        super().__init__(
            domain=domain,
            agent=agent,
            user=user,
            environment=environment,
            task=task,
            max_steps=max_steps,
            max_errors=max_errors,
            seed=seed,
            simulation_id=simulation_id,
            timeout=timeout,
        )

        # Half-duplex specific attributes
        self.mode = CommunicationMode.HALF_DUPLEX
        self.trajectory: list[Message] = []
        self.solo_mode = solo_mode
        self.validate_communication = validate_communication

        # Turn-based routing state
        self.from_role: Optional[Role] = None
        self.to_role: Optional[Role] = None
        self.message: Optional[Message] = None

        # Pending-action reminder scaffolding (off by default)
        self.pending_action_reminder_interval = pending_action_reminder_interval
        self.pending_action_reminder_start = pending_action_reminder_start
        self.pending_action_reminder_use_ledger_state = (
            pending_action_reminder_use_ledger_state
        )
        self.pending_action_reminder_workflow_names = (
            pending_action_reminder_workflow_names
        )
        self.pending_action_reminder_workflow_triggers = (
            pending_action_reminder_workflow_triggers
        )
        self.pending_action_reminder_delivery = pending_action_reminder_delivery
        self.pending_action_reminder_checklist_scope = (
            pending_action_reminder_checklist_scope
        )
        self.self_verification_reminder = self_verification_reminder
        # Plan pinning (see PINNED_PLAN_TEMPLATE). None = the agent writes its
        # own plan, i.e. the shipped behavior and the ablation's control arm.
        self.pinned_plan = pinned_plan
        self.pinned_plan_steps = pinned_plan_steps
        # Checklist channel: False = the <workflow_checklist> text block (the
        # shipped behavior, and the control arm), True = the `update_checklist`
        # tool. The tool must ALSO be added to the agent's tool list by the
        # caller -- see checklist_tool.get_checklist_tool(). This flag only
        # changes what the harness asks for and what it accepts; it does not
        # reach into the agent.
        self.checklist_tool = checklist_tool
        # How often the harness ASKS for a checklist.
        #
        #   every_turn  the shipped behavior: CLASSIFY, then a REVIEW echo on
        #               every subsequent fire, plus a forced resample when no
        #               checklist exists yet.
        #   once        the invitation is issued exactly once and then the
        #               harness goes quiet. No review echo, no re-ask, no
        #               enforcement retry. The tool stays available, so calling
        #               it again -- or never again -- is the model's decision.
        #
        # "once" exists because of what the first H run showed (n=2/arm, so
        # directional only): the tool arm was reminded 9 and 15 times, never
        # complied on the first try, and both episodes ran 26 turns into
        # max_steps -- while the text arm complied first try and finished
        # normally at 17 and 10 turns. A half-duplex agent emits one tool call
        # per turn, so every turn spent on update_checklist is a turn not spent
        # on the task. Nagging every turn and then measuring turn count
        # confounds "the tool costs turns" with "the harness demanded it every
        # turn". This separates them.
        self.checklist_prompt = checklist_prompt
        # Splits the reminder from the FORCED RESAMPLE. Until 2026-07-28 these
        # were one setting: prompting="every_turn" meant "re-ask every turn AND
        # roll back + resample any turn that didn't comply", prompting="once"
        # meant neither. So every comparison between those two arms moved two
        # variables at once, and the gap could never be attributed to either.
        #
        # It matters now because the 2026-07-28 probe made enforcement look
        # nearly useless: 14 enforced turns, 11 forced resamples, 1 that
        # produced a checklist (9%). Nine of the arm's ten checklists appeared
        # WITHOUT being forced -- i.e. the reminder alone may be doing the work
        # and the resample may be pure cost. "May": with the old two-in-one
        # setting that reading is unfalsifiable, which is the reason for this
        # flag rather than for deleting the mechanism outright.
        self.checklist_enforce = checklist_enforce
        self.planner_model = planner_model
        self.planner_domain_policy = planner_domain_policy
        self.planner_llm_args = planner_llm_args
        self._planner_result = None
        self._pinned_plan_delivered: bool = False
        self._pinned_plan_skills: list[str] = []
        self._agent_turns_seen: int = 0
        self._workflow_checklist_established: bool = False
        # Structured checklist state, parsed from the agent's own reply text (see
        # _parse_and_store_workflow_checklist). One entry per named skill the agent
        # has declared; each entry's `items` is [{"text": str, "done": bool}, ...].
        # This is real orchestrator-held state -- not just a hope that the model
        # remembers what it said upthread -- so REVIEW turns can echo back the last
        # known state verbatim instead of asking the model to recall it.
        self._workflow_checklists: list[dict] = []
        # Set by _build_agent_input_message on any turn where the CLASSIFY or
        # REVIEW_NONE template fired (i.e. a checklist is expected but none is
        # established yet) -- step() checks this after the agent responds and
        # forces one bounded retry if the model still didn't write one. Not
        # set (stays False) on REVIEW turns (a checklist already exists) or
        # turns where the reminder didn't fire at all -- v1 of this
        # enforcement only covers the "zero checklists established yet"
        # case, which is where the compliance gap was actually measured
        # (see FAILURE_ANALYSIS/.../orchestrator_reminder_scoping.md's
        # 2026-07-26 update: 37 of 39 "should have triggered" cases across
        # Tier 1/2 never got any checklist at all, not a stale one).
        self._checklist_enforcement_pending: bool = False
        #: update_checklist calls whose arguments carried no usable steps. Kept
        #: separate from "never called it" -- the two demand different fixes.
        self._checklist_tool_bad_calls: int = 0
        #: update_checklist calls executed, usable or not.
        self._checklist_tool_calls: int = 0
        # Running log of every checklist compliance check this episode --
        # (agent_turn, was_enforced, retry_used, complied_after_retry) --
        # persisted into SimulationRun.info so pass/fail on this mechanism
        # is a queryable fact, not something to reconstruct by regexing
        # message content after the run.
        self._checklist_enforcement_log: list[dict] = []
        # Full history (not just latest state) for post-hoc analysis:
        # - _matched_skills_history: one entry per reminder fire, which named
        #   skills the customer's own words matched at that point (per
        #   _matched_workflow_skill_names) -- lets analysis see *when* a new
        #   skill first got detected (turn 0 vs mid-conversation), not just
        #   the union at episode end.
        # - _workflow_checklist_history: one entry per <workflow_checklist>
        #   block actually parsed out of an agent turn (see
        #   _parse_and_store_workflow_checklist), even though
        #   self._workflow_checklists itself only keeps the latest per skill.
        self._matched_skills_history: list[dict] = []
        self._workflow_checklist_history: list[dict] = []

        # Validate mode compatibility
        self._validate_mode_compatibility()

    def _validate_mode_compatibility(self):
        """
        Validate that the agent and user support half-duplex communication.

        Raises:
            ValueError: If agent or user don't support half-duplex mode.
        """
        if not hasattr(self.agent, "generate_next_message"):
            raise ValueError(
                f"Agent {self.agent.__class__.__name__} must have 'generate_next_message' method."
            )

        if not hasattr(self.user, "generate_next_message"):
            raise ValueError(
                f"User {self.user.__class__.__name__} must have 'generate_next_message' method."
            )

        logger.info(
            f"Orchestrator initialized in HALF_DUPLEX mode (turn-based) with "
            f"agent={self.agent.__class__.__name__}, "
            f"user={self.user.__class__.__name__}"
        )

    def initialize(self):
        """
        Initialize the orchestrator.
        - If the tasks specifies an initial state, use it to initialize the environment.
        - Initialize the agent and user states.
        - Send the first message (default message from the agent to the user).
        """
        initial_state = self.task.initial_state
        initialization_data = (
            initial_state.initialization_data if initial_state is not None else None
        )
        initialization_actions = (
            initial_state.initialization_actions if initial_state is not None else None
        )
        message_history = (
            deepcopy(initial_state.message_history)
            if initial_state is not None and initial_state.message_history is not None
            else []
        )
        for msg in message_history:
            msg.turn_idx = None

        # Add timestamps to the message history
        message_history = self._add_timestamps(message_history)

        if self.solo_mode:
            assert self.environment.solo_mode, "Environment should be in solo mode"
            assert (
                isinstance(self.agent, LLMSoloAgent)
                or self.agent.__class__.__name__ == "GymAgent"
            ), "Agent must be a LLMSoloAgent or GymAgent in solo mode"
            assert isinstance(self.user, DummyUser), (
                "User must be a DummyUser in solo mode"
            )

        # Initialize Environment state
        self._initialize_environment(
            initialization_data=initialization_data,
            initialization_actions=initialization_actions,
            message_history=message_history,
        )

        # Set seeds for the agent, user
        if self.seed is not None:
            self.agent.set_seed(self.seed)
            self.user.set_seed(self.seed)

        # Initialize the agent and user states
        if len(message_history) > 0:
            self.validate_message_history(message_history)

            last_message = message_history[-1]
            # Last message is an assistant message
            if isinstance(last_message, AssistantMessage):
                self.from_role = Role.AGENT
                if not last_message.is_tool_call():  # Last message is for the user
                    self.to_role = Role.USER
                else:  # Last message is for the environment
                    self.to_role = Role.ENV
                self.agent_state = self.agent.get_init_state(
                    message_history=[
                        msg
                        for msg in message_history
                        if is_valid_agent_history_message(msg)
                    ]
                )
                self.user_state = self.user.get_init_state(
                    message_history=[
                        msg
                        for msg in message_history[:-1]
                        if is_valid_user_history_message(msg)
                    ]
                )
                self.message = last_message
                if self.agent.is_stop(last_message):
                    self.done = True
                    self.termination_reason = TerminationReason.AGENT_STOP
            # Last message is a user message
            elif isinstance(last_message, UserMessage):
                self.from_role = Role.USER
                if not last_message.is_tool_call():  # Last message is for the agent
                    self.to_role = Role.AGENT
                else:  # Last message is for the environment
                    self.to_role = Role.ENV
                self.user_state = self.user.get_init_state(
                    message_history=[
                        msg
                        for msg in message_history
                        if is_valid_user_history_message(msg)
                    ]
                )
                self.agent_state = self.agent.get_init_state(
                    message_history=[
                        msg
                        for msg in message_history[:-1]
                        if is_valid_agent_history_message(msg)
                    ]
                )
                self.message = last_message
                self.done = UserSimulator.is_stop(last_message)
                if self.done:
                    self.termination_reason = TerminationReason.USER_STOP
            # Last message is a tool message
            elif isinstance(last_message, ToolMessage):
                self.from_role = Role.ENV
                if last_message.requestor == "assistant":
                    self.to_role = Role.AGENT
                    self.agent_state = self.agent.get_init_state(
                        message_history=[
                            msg
                            for msg in message_history[:-1]
                            if is_valid_agent_history_message(msg)
                        ]
                    )
                    self.user_state = self.user.get_init_state(
                        message_history=[
                            msg
                            for msg in message_history
                            if is_valid_user_history_message(msg)
                        ]
                    )
                else:
                    self.to_role = Role.USER
                    self.agent_state = self.agent.get_init_state(
                        message_history=[
                            msg
                            for msg in message_history
                            if is_valid_agent_history_message(msg)
                        ]
                    )
                    self.user_state = self.user.get_init_state(
                        message_history=[
                            msg
                            for msg in message_history[:-1]
                            if is_valid_user_history_message(msg)
                        ]
                    )
                self.message = last_message
            else:
                raise ValueError(
                    f"Last message should be of type AssistantMessage, UserMessage, or ToolMessage, got {type(last_message)}"
                )
            self.trajectory = message_history
        else:
            # No message history - initialize fresh
            self.user_state = self.user.get_init_state()
            if not self.solo_mode:
                first_message = deepcopy(DEFAULT_FIRST_AGENT_MESSAGE)
                first_message.timestamp = get_now()
                self.agent_state = self.agent.get_init_state(
                    message_history=[first_message]
                )
                self.trajectory = [first_message]
                self.message = first_message
                self.from_role = Role.AGENT
                self.to_role = Role.USER
            else:
                self.agent_state = self.agent.get_init_state()
                first_message, self.agent_state = self.agent.generate_next_message(
                    None, self.agent_state
                )
                self.trajectory = [first_message]
                self.message = first_message
                # In solo mode, there is no user, so if the message is not a tool call, then we end and report an agent error
                if not first_message.is_tool_call():
                    self.from_role = Role.AGENT
                    self.to_role = Role.USER
                    self.done = True
                    if self.agent.is_stop(first_message):
                        # If the agent is stopping (###STOP###)
                        self.termination_reason = TerminationReason.AGENT_STOP
                    else:
                        self.termination_reason = TerminationReason.AGENT_ERROR
                else:
                    self.from_role = Role.AGENT
                    self.to_role = Role.ENV
                    self.done = self.agent.is_stop(first_message)
                    if self.done:
                        self.to_role = Role.USER  # FIXIT: For now, we assume last message cannot be to the environment
                        self.termination_reason = TerminationReason.AGENT_STOP

        if self.validate_communication:
            self.check_communication_error()
        self.environment.sync_tools()

    def check_communication_error(self) -> None:
        """
        Check the orchestrator state for communication errors and handle them appropriately.

        Communication errors occur when agents or users violate the communication protocol rules:
        - Empty messages (no text content and no tool calls)
        - Mixed messages (both text content and tool calls in the same message)
        - Solo mode violations (agents sending text content instead of tool calls)

        When a communication error is detected:
        - Sets `self.done = True` to terminate the simulation
        - Sets `self.termination_reason` to either `AGENT_ERROR` or `USER_ERROR`
        - Re-raises any other exceptions that are not communication-related
        """
        try:
            self._check_communication_error()
        except AgentError:
            self.done = True
            self.termination_reason = TerminationReason.AGENT_ERROR
        except UserError:
            self.done = True
            self.termination_reason = TerminationReason.USER_ERROR
        except Exception:
            # Re-raise all other exceptions
            raise

    def _check_communication_error(self) -> None:
        """
        Check the orchestrator state for communication protocol violations.

        Validates that messages follow the communication rules:
        1. Messages must have either text content OR tool calls, not both
        2. Messages cannot be empty (no text content and no tool calls)
        3. In solo mode, agents can only send tool calls (except for stop messages)

        Raises:
            AgentError: When the agent violates communication rules
            UserError: When the user violates communication rules
            ValueError: When from_role is invalid
        """
        if self.from_role == Role.ENV:
            return
        if self.from_role == Role.USER:
            exception_type = UserError
        elif self.from_role == Role.AGENT:
            exception_type = AgentError
        else:
            raise ValueError(f"Invalid from role: {self.from_role}")
        # Check if the message is empty
        if not self.message.is_tool_call() and not self.message.has_text_content():
            raise exception_type(
                f"{self.from_role.value} sent an empty message. {self.message}"
            )
        # Check if the message has both text content and tool calls
        if self.message.is_tool_call() and self.message.has_text_content():
            raise exception_type(
                f"{self.from_role.value} sent both text content and tool calls. {self.message}"
            )

        # Check if the agent is allowed to send a message to the user
        if self.from_role == Role.AGENT and self.solo_mode:
            if self.message.has_text_content() and not self.agent.is_stop(self.message):
                raise exception_type(
                    f"{self.from_role.value} can only send tool calls. {self.message}"
                )

    def _check_termination(self) -> None:
        """
        Check for half-duplex specific termination conditions.

        Only checks max_steps/max_errors/timeout when not waiting for environment response.
        """
        # Skip termination checks if we're waiting for environment to respond
        if self.to_role == Role.ENV:
            return

        if self.step_count >= self.max_steps:
            self.done = True
            self.termination_reason = TerminationReason.MAX_STEPS
        if self.num_errors >= self.max_errors:
            self.done = True
            self.termination_reason = TerminationReason.TOO_MANY_ERRORS
        self._check_timeout()

    def _finalize(self) -> SimulationRun:
        """
        Finalize the half-duplex simulation and create the SimulationRun result.

        Sends stop signals to agent and user, calculates costs, and builds the result.

        Returns:
            SimulationRun with all simulation data.
        """
        # Send stop signal to the agent, user, and environment
        has_error = self.termination_reason in [
            TerminationReason.USER_ERROR,
            TerminationReason.AGENT_ERROR,
        ]

        last_msg_to_agent = None
        last_msg_to_user = None
        if self.to_role == Role.AGENT:
            last_msg_to_agent = self.message
        elif self.to_role == Role.USER:
            last_msg_to_user = self.message
        elif self.to_role == Role.ENV and not has_error:
            raise ValueError(
                "Environment should not receive the last message. Last message: "
                + str(self.message)
            )
        try:
            self.agent.stop(last_msg_to_agent, self.agent_state)
        except Exception as e:
            logger.warning(f"Error stopping agent during finalization: {e}")
        try:
            self.user.stop(last_msg_to_user, self.user_state)
        except Exception as e:
            logger.warning(f"Error stopping user during finalization: {e}")

        # Wrap up the simulation
        duration = time.perf_counter() - self._run_start_perf
        messages = self.get_trajectory()
        res = get_cost(messages)
        if res is None:
            agent_cost, user_cost = None, None
        else:
            agent_cost, user_cost = res
        # Update voice metadata with final turn_idx values
        self._finalize_voice_metadata(messages)

        # Get speech_environment from user's voice_settings if available
        speech_environment = None
        if (
            hasattr(self.user, "voice_settings")
            and self.user.voice_settings is not None
        ):
            speech_environment = self.user.voice_settings.speech_environment

        # Persist the pending-action-reminder / workflow-checklist mechanism's
        # final state so post-hoc analysis doesn't have to regex-scan message
        # content to reconstruct whether (and for which skill) the checklist
        # was ever actually triggered -- self._workflow_checklists only ever
        # holds each skill's latest snapshot (see
        # _parse_and_store_workflow_checklist), so a non-empty entry here
        # means that skill's checklist was written at least once this
        # episode; an empty dict means the reminder never resulted in one,
        # regardless of how many times it fired.
        #
        # workflow_matches: one entry per NAMED skill that ever matched the
        # customer's own words at any reminder fire (per
        # _matched_workflow_skill_names) -- when it first/last matched, how
        # many separate reminder-fires saw it matched, and whether a
        # checklist was ever actually established for it (name match from a
        # keyword hit alone doesn't mean the agent tracked it -- that's
        # exactly the gap this field exists to make visible).
        #
        # checklists: one entry per skill name the agent itself ever wrote a
        # <workflow_checklist> block for (named or ad hoc) -- how many times
        # it was updated, the full item-count/done-count trajectory across
        # updates (not just the final snapshot), and whether the last
        # recorded state has every item checked off.
        info = None
        if self.pending_action_reminder_interval is not None:
            all_matched_skills: set[str] = set()
            for h in self._matched_skills_history:
                all_matched_skills.update(h["matched_skills"])
            checklist_skills_ever = {
                h["skill"] for h in self._workflow_checklist_history
            }

            workflow_matches = {}
            for skill in sorted(all_matched_skills):
                fires = [
                    h["agent_turn"]
                    for h in self._matched_skills_history
                    if skill in h["matched_skills"]
                ]
                final_entry = next(
                    (c for c in self._workflow_checklists if c["skill"] == skill), None
                )
                workflow_matches[skill] = {
                    "matched_at_turns": fires,
                    "n_matches": len(fires),
                    "first_matched_turn": min(fires) if fires else None,
                    "checklist_ever_written_for_this_skill": skill
                    in checklist_skills_ever,
                    "final_items_total": len(final_entry["items"])
                    if final_entry
                    else None,
                    "final_items_done": (
                        sum(1 for it in final_entry["items"] if it["done"])
                        if final_entry
                        else None
                    ),
                    "fully_completed": (
                        all(it["done"] for it in final_entry["items"])
                        if final_entry and final_entry["items"]
                        else None
                    ),
                }

            checklists = {}
            for skill in sorted(
                {c["skill"] for c in self._workflow_checklists} | checklist_skills_ever
            ):
                updates = [
                    h for h in self._workflow_checklist_history if h["skill"] == skill
                ]
                final_entry = next(
                    (c for c in self._workflow_checklists if c["skill"] == skill), None
                )
                checklists[skill] = {
                    "triggered": bool(updates),
                    "n_updates": len(updates),
                    "update_turns": [u["agent_turn"] for u in updates],
                    # CHURN. `items` was dropped here until 2026-07-29, and that
                    # is why the churn diagnostic that
                    # docs/agent_checklist_planning_research.md §555 calls a
                    # prerequisite ("没有这两个数,无法判断任何改动是否有效")
                    # had never been computed from an eval run: the revision
                    # COUNT survived to disk, the revision CONTENT did not. So
                    # "did the plan actually change, and did the change help"
                    # was unanswerable from any stored simulation. The number
                    # that finally answered it (42% of multi-checklist episodes
                    # re-send an identical list) had to be recovered from
                    # training `transcripts.jsonl`, which only training runs
                    # write -- eval runs produced nothing usable.
                    #
                    # Storing the text is the honest fix rather than a hash:
                    # the question is not only "did it change" but "did the
                    # change move it toward the gold trajectory", and that needs
                    # the text to re-score. Size is a non-issue next to the full
                    # message list already in this file.
                    #
                    # `text_changed` is precomputed against the PREVIOUS update
                    # of the same skill so a reader gets churn without
                    # reimplementing the comparison (and without silently
                    # choosing a different normalisation than this one).
                    "items_over_time": [
                        {
                            "agent_turn": u["agent_turn"],
                            "n_items": u["n_items"],
                            "n_done": u["n_done"],
                            "items": [it.get("text", "") for it in u["items"]],
                            "text_changed": (
                                None
                                if i == 0
                                else [it.get("text", "") for it in u["items"]]
                                != [
                                    it.get("text", "") for it in updates[i - 1]["items"]
                                ]
                            ),
                        }
                        for i, u in enumerate(updates)
                    ],
                    # Revisions that changed nothing but the done-flags. A high
                    # value against n_updates means the harness's per-turn echo
                    # is producing re-sends, not re-planning.
                    "n_text_revisions": sum(
                        1
                        for i, u in enumerate(updates)
                        if i > 0
                        and [it.get("text", "") for it in u["items"]]
                        != [it.get("text", "") for it in updates[i - 1]["items"]]
                    ),
                    "final_items_total": len(final_entry["items"])
                    if final_entry
                    else 0,
                    "final_items_done": (
                        sum(1 for it in final_entry["items"] if it["done"])
                        if final_entry
                        else 0
                    ),
                    "fully_completed": (
                        bool(
                            final_entry
                            and final_entry["items"]
                            and all(it["done"] for it in final_entry["items"])
                        )
                    ),
                }

            info = {
                "workflow_checklist_established": self._workflow_checklist_established,
                "workflow_matches": workflow_matches,
                "checklists": checklists,
                "checklist_enforcement_log": self._checklist_enforcement_log,
                # Which channel was offered, and how the model used it. Recorded
                # on every episode (False/0 in text mode) so a tool-vs-text
                # comparison can be reassembled from stored records without
                # relying on run-directory naming. `checklist_sources` is what
                # answers "did the tool arm actually use the tool, or did it
                # keep writing blocks" -- a tool arm whose checklists all came
                # from `content` is not a tool-arm observation.
                "checklist_tool_mode": self.checklist_tool,
                "checklist_prompt": self.checklist_prompt,
                "checklist_enforce": self.checklist_enforce,
                "checklist_tool_calls": self._checklist_tool_calls,
                "checklist_tool_bad_calls": self._checklist_tool_bad_calls,
                "checklist_sources": {
                    src: sum(
                        1
                        for h in self._workflow_checklist_history
                        if h.get("source") == src
                    )
                    for src in ("tool_call", "thinking", "content")
                },
                # Plan-pinning arm. Recorded on EVERY episode (None for the
                # control arm) so the ablation can be reassembled from stored
                # records without depending on run-directory naming.
                #
                # pinned_plan_empty is the one that matters for correctness:
                # an oracle episode where no skill matched had nothing to pin,
                # so it is NOT an oracle observation and must be excluded
                # before computing R(oracle). Pooling those in would dilute the
                # arm with control-arm episodes wearing an oracle label.
                "pinned_plan": self.pinned_plan,
                "pinned_plan_skills": self._pinned_plan_skills,
                "pinned_plan_empty": bool(
                    self.pinned_plan and not self._pinned_plan_skills
                ),
                "agent_wrote_own_checklist_despite_pin": bool(
                    self.pinned_plan
                    and any(
                        h.get("superseded_by_pinned_plan")
                        for h in self._workflow_checklist_history
                    )
                ),
            }

        simulation_run = SimulationRun(
            id=self.simulation_id,
            task_id=self.task.id,
            start_time=self._run_start_time,
            end_time=get_now(),
            duration=duration,
            termination_reason=self.termination_reason.value,
            reward_info=None,
            user_cost=user_cost,
            agent_cost=agent_cost,
            messages=messages,
            seed=self.seed,
            mode=self.mode.value,
            speech_environment=speech_environment,
            info=info,
        )
        return simulation_run

    def _deliver_reminder_as_system_message(
        self, reminder_text: str, marker: Optional[str] = None
    ) -> bool:
        """
        Insert or update a dedicated SystemMessage in self.agent_state.system_messages
        carrying `reminder_text`, instead of embedding it inside the turn's
        UserMessage/ToolMessage content.

        Tests a hypothesis raised directly against the "message" delivery mode:
        that a model may weigh system-role content (where the domain's own base
        policy guidelines live) more heavily than the same text appended inside a
        user/tool turn, independent of how many times it's repeated. Untested
        until this method existed -- "message" delivery was the only mode
        available through 2026-07-22.

        Duck-typed on `system_messages` being a `list` attribute of agent_state
        (true for LLMAgentState, see llm_agent.py) -- returns False for any agent
        state that doesn't expose one, so the caller can fall back to "message"
        delivery for that turn rather than silently doing nothing.

        Idempotent: finds the previously-inserted reminder SystemMessage by its
        marker prefix and updates its .content in place on subsequent calls,
        rather than appending a new one every time (which would grow
        system_messages unboundedly over a long episode).
        """
        marker = marker or _HARNESS_SYSTEM_REMINDER_MARKER
        system_messages = getattr(self.agent_state, "system_messages", None)
        if not isinstance(system_messages, list):
            return False
        marked_content = f"{marker}\n{reminder_text}"
        for sm in system_messages:
            if isinstance(sm, SystemMessage) and (sm.content or "").startswith(marker):
                sm.content = marked_content
                return True
        system_messages.append(SystemMessage(role="system", content=marked_content))
        return True

    def _matched_workflow_skill_names(self) -> set[str]:
        """
        Detection gate for pending_action_reminder_workflow_names, driven by
        pending_action_reminder_workflow_triggers (see its constructor docstring).

        Scans ONLY the customer's own words -- every UserMessage.content seen so
        far in self.trajectory, concatenated and lowercased -- never tool-call
        results or environment/ledger state. This is deliberate: several of this
        domain's tools are shared across multiple named skillsets (e.g.
        get_user_dispute_history_7291 appears in the CLI, dispute-filing, and
        lost-wallet skillsets), so gating on tool names or results would misfire
        whenever any of those shared tools gets called for an unrelated reason.
        Gating on what the customer actually said avoids that.

        Re-scans the full customer-utterance history on every call rather than
        caching a turn-0 result, so a second request that surfaces mid-
        conversation still gets picked up on the next reminder firing.

        A skill name with no entry in pending_action_reminder_workflow_triggers
        is always included in the result -- this gate only narrows *situational*
        skills, it doesn't suppress a skill whose caller deliberately didn't give
        it keywords (e.g. "identity verification", whose own Triggers-on
        condition is near-universal and was never meant to be gated).
        """
        names = self.pending_action_reminder_workflow_names or []
        triggers = self.pending_action_reminder_workflow_triggers
        if not triggers:
            return set(names)

        customer_text = " ".join(
            msg.content
            for msg in self.trajectory
            if isinstance(msg, UserMessage) and msg.content
        ).lower()

        matched: set[str] = set()
        for name in names:
            keyword_groups = triggers.get(name)
            if not keyword_groups:
                matched.add(name)
                continue
            if any(
                all(keyword.lower() in customer_text for keyword in group)
                for group in keyword_groups
            ):
                matched.add(name)
        return matched

    def _build_pinned_checklists(self) -> list[dict]:
        """Materialise the pinned plan for THIS turn, in the same shape
        _workflow_checklists uses ({"skill", "items":[{"text","done"}]}).

        Built lazily rather than in __init__ because the oracle arm depends on
        which skills the customer's own words have matched so far, and that is
        not knowable at construction time -- a second request surfacing
        mid-conversation should widen the pinned plan the same way it widens
        the live gate's match set.

        Returns [] when the oracle arm has nothing to pin (no skill matched, or
        no steps defined for the matched skill). That is deliberate and must be
        recorded rather than silently backfilled with the null plan: an oracle
        episode with an empty pin is not an oracle observation, and pooling it
        with real ones would dilute exactly the effect the arm exists to
        measure. `pinned_plan_empty` in the telemetry flags these for exclusion.
        """
        if self.pinned_plan == "gold":
            # TEACHER FORCING -- READS THE GRADED ANSWER.
            #
            # Unlike every other arm here, these steps come from the task's
            # `evaluation_criteria.actions` (built by
            # FAILURE_ANALYSIS/planning_eval/build_gold_checklists.py). That is
            # leakage by construction, and it is legitimate in exactly one
            # place: a TRAINING rollout, where showing the answer is the point.
            #
            # A solve rate from an eval run with this arm active is not a
            # measurement of anything. The builder refuses to emit held-out
            # tasks for that reason, so an unknown task_id here means the
            # episode is outside the training split -- pin nothing and let it be
            # flagged pinned_plan_empty rather than quietly falling back to some
            # other plan.
            plan = (self.pinned_plan_steps or {}).get("tasks", {}).get(self.task.id)
            if not plan or not plan.get("steps"):
                return []
            return [
                {
                    "skill": plan.get("skill") or self.task.id,
                    "items": [
                        {"text": step.get("text", ""), "done": False}
                        for step in plan["steps"]
                        if step.get("text")
                    ],
                }
            ]

        if self.pinned_plan == "swap":
            # ANOTHER TASK'S GOLD PLAN. The third arm of the counterfactual
            # ablation in docs/agent_checklist_planning_research.md §11, and the
            # strictest one: `null` only removes the plan, so R(control)-R(null)
            # is still consistent with "any block of plan-shaped tokens warms the
            # model up". A swapped plan is the same length, the same format and
            # the same quality as the real one and differs ONLY in relevance, so
            # it isolates whether the agent is FOLLOWING the plan or merely
            # accompanied by one.
            #
            # It reads the same file as the `gold` arm on purpose. Any other
            # donor (a hand-written decoy, a plan from workflow_steps.json)
            # would differ from the control plan in more than relevance, and the
            # difference would be uninterpretable.
            #
            # DONOR CHOICE IS DETERMINISTIC -- the next task id in sorted order,
            # wrapping around -- so the arm reproduces exactly across seeds and
            # reruns, and the pairing can be audited after the fact. The donor
            # is recorded in telemetry (`pinned_plan_skills`) rather than left
            # to be inferred from the transcript.
            tasks = (self.pinned_plan_steps or {}).get("tasks", {})
            ids = sorted(tasks)
            if len(ids) < 2:
                return []
            try:
                donor = ids[(ids.index(self.task.id) + 1) % len(ids)]
            except ValueError:
                # This task is not in the file (held-out). Pinning an arbitrary
                # donor would still be a valid swap, but it would make the arm's
                # task set differ from `gold`'s, and the comparison needs them
                # identical. Pin nothing; pinned_plan_empty flags it.
                return []
            plan = tasks.get(donor)
            if not plan or not plan.get("steps"):
                return []
            return [
                {
                    # The skill label carries the donor id so a reader of the
                    # transcript can see at a glance which plan was substituted.
                    "skill": f"swapped-from:{donor}",
                    "items": [
                        {"text": step.get("text", ""), "done": False}
                        for step in plan["steps"]
                        if step.get("text")
                    ],
                }
            ]

        if self.pinned_plan == "null":
            return [
                {
                    "skill": NULL_PLAN_SKILL,
                    "items": [{"text": t, "done": False} for t in NULL_PLAN_ITEMS],
                }
            ]

        if self.pinned_plan == "planner":
            # Schema-constrained planning call. Computed ONCE per episode and
            # cached: re-planning every turn would make the plan a moving target
            # and cost a call per turn, and this arm is testing whether a
            # well-formed plan helps -- not whether re-planning helps.
            if self._planner_result is None:
                from tau2.orchestrator.planner import make_plan

                self._planner_result = make_plan(
                    self.trajectory,
                    model=self.planner_model or "",
                    domain_policy=self.planner_domain_policy,
                    extra_llm_args=self.planner_llm_args,
                )
                if not self._planner_result.ok:
                    logger.warning(
                        f"planner produced no plan ({self._planner_result.error}); "
                        "this episode is NOT a planner-arm observation and is "
                        "flagged pinned_plan_empty for exclusion."
                    )
            return self._planner_result.as_checklist()

        if self.pinned_plan != "oracle":
            return []

        steps_by_skill = (self.pinned_plan_steps or {}).get("skillsets", {})
        if not steps_by_skill:
            return []

        out: list[dict] = []
        for skill in sorted(self._matched_workflow_skill_names()):
            spec = steps_by_skill.get(skill)
            if not spec:
                continue
            items = [
                {"text": step.get("text", ""), "done": False}
                for step in spec.get("steps", [])
                if step.get("text")
            ]
            if items:
                out.append({"skill": skill, "items": items})
        return out

    def _any_tool_call_made(self) -> bool:
        """
        Gate for SELF_VERIFICATION_REMINDER: has the agent made at least one tool
        call so far this session? Purely mechanical -- checks self.trajectory for
        any AssistantMessage with a non-empty tool_calls list, regardless of which
        tool or domain. Not derived from golden/expected actions, so it carries no
        leakage risk (same as _pending_discoverable_tool_note's ledger check).
        Before any tool call has happened there is nothing yet to verify, so the
        reminder should stay silent.
        """
        for msg in self.trajectory:
            if getattr(msg, "tool_calls", None):
                return True
        return False

    def _build_agent_input_message(self) -> Message:
        """
        Return the message to hand to the agent this turn: self.message unchanged,
        or -- if a pending-action reminder is enabled and due -- a CLONE of
        self.message with PENDING_ACTION_REMINDER appended to its content.

        MUST return a clone, never mutate self.message in place. self.message is
        the exact same object already stored in self.trajectory, and -- when it
        originated from the other participant's own last turn -- in that
        participant's own state.messages too: both HalfDuplexAgent and
        UserSimulator's generate_next_message() do `state.messages.append(message)`
        with the very object they were just handed. Mutating self.message.content
        in place therefore leaks the reminder text backward into the sender's own
        remembered conversation history. Confirmed via a real eval transcript
        (task_016, 2026-07-22) where the user-sim's own next turn quoted
        "the harness reminder says..." while reasoning about its OWN prior line,
        because that line had been silently rewritten out from under it. The clone
        is used only for this one call to agent.generate_next_message();
        self.trajectory and self.message keep the pristine, unmodified content the
        sender actually produced.
        """
        if self.pending_action_reminder_interval is None:
            return self.message
        turns_since_start = self._agent_turns_seen - self.pending_action_reminder_start
        should_fire = turns_since_start >= 0 and (
            turns_since_start % self.pending_action_reminder_interval == 0
        )
        fired_at_turn = self._agent_turns_seen
        self._agent_turns_seen += 1
        if not should_fire:
            return self.message

        # Detection gate (2026-07-22): only the named skills the customer's own
        # words actually match (per pending_action_reminder_workflow_triggers)
        # get surfaced. In "named_only" scope this also gates whether the
        # checklist mechanism engages at all -- zero matches means no classify
        # prompt, just the plain ledger-based reminder below, matching a clean
        # Q&A-only conversation's expected silence. In "universal" scope the
        # checklist stays active regardless (its whole point is an ad hoc
        # fallback for unmatched requests); only which named skills get
        # mentioned in the hint is narrowed.
        matched_names = self._matched_workflow_skill_names()
        checklist_active = bool(matched_names) or (
            self.pending_action_reminder_checklist_scope == "universal"
        )
        self._matched_skills_history.append(
            {"agent_turn": fired_at_turn, "matched_skills": sorted(matched_names)}
        )

        reminder_lines = [] if checklist_active else [PENDING_ACTION_REMINDER]
        if self.pending_action_reminder_use_ledger_state:
            detail = self._pending_discoverable_tool_note()
            if detail:
                reminder_lines.append(detail)
        if checklist_active:
            matched_ordered = [
                n
                for n in (self.pending_action_reminder_workflow_names or [])
                if n in matched_names
            ]
            named_hint = (
                WORKFLOW_SKILL_NAMED_HINT_TEMPLATE.format(
                    names=", ".join(matched_ordered)
                )
                if matched_ordered
                else ""
            )
            if self.pinned_plan:
                # Pinned arms never ask the agent to author a plan, so the
                # CLASSIFY prompt and its enforcement retry are both skipped --
                # firing them would reintroduce the very "will the model write
                # the format" variable the pinning exists to hold constant.
                # Rebuilt every fire so a mid-conversation second request
                # widens the oracle plan, matching the live gate's behavior.
                pinned = self._build_pinned_checklists()
                if pinned:
                    self._workflow_checklists = pinned
                    self._workflow_checklist_established = True
                    template = (
                        PINNED_PLAN_TEMPLATE
                        if not self._pinned_plan_delivered
                        else self._review_template()
                    )
                    reminder_lines.append(
                        template.format(checklist=self._format_workflow_checklists())
                    )
                    self._pinned_plan_delivered = True
                    self._pinned_plan_skills = [c["skill"] for c in pinned]
                # No else: an oracle arm with nothing to pin stays silent rather
                # than falling back to the CLASSIFY prompt. Falling back would
                # quietly turn those episodes into control-arm episodes wearing
                # an oracle label.
            elif not self._workflow_checklist_established:
                if not self.checklist_tool:
                    classify = WORKFLOW_SKILL_CLASSIFY_TEMPLATE
                elif self.checklist_prompt == "once":
                    classify = WORKFLOW_SKILL_CLASSIFY_TOOL_ONCE_TEMPLATE
                else:
                    classify = WORKFLOW_SKILL_CLASSIFY_TOOL_TEMPLATE
                reminder_lines.append(classify.format(named_hint=named_hint))
                self._workflow_checklist_established = True
                # "once" mode never forces: the invitation is issued one time and
                # the model is left to decide. See checklist_prompt's docstring.
                # checklist_enforce=False keeps the every-turn reminder but drops
                # the forced resample -- the third arm.
                if self.checklist_prompt != "once" and self.checklist_enforce:
                    self._checklist_enforcement_pending = True
            elif self.checklist_prompt == "once":
                # Invitation already issued. Say nothing further -- no review
                # echo, no re-ask, no enforcement. The tool stays in the tool
                # list, so the model can call it whenever it judges it useful.
                pass
            elif self._workflow_checklists:
                reminder_lines.append(
                    self._review_template().format(
                        checklist=self._format_workflow_checklists()
                    )
                )
            else:
                reminder_lines.append(
                    WORKFLOW_SKILL_REVIEW_NONE_TOOL_TEMPLATE
                    if self.checklist_tool
                    else WORKFLOW_SKILL_REVIEW_NONE_TEMPLATE
                )
                if self.checklist_enforce:
                    self._checklist_enforcement_pending = True
        if self.self_verification_reminder and self._any_tool_call_made():
            reminder_lines.append(SELF_VERIFICATION_REMINDER)
        reminder_text = "\n".join(reminder_lines)

        if self.pending_action_reminder_delivery == "system" and (
            self._deliver_reminder_as_system_message(reminder_text)
        ):
            logger.debug(
                f"Step {self.step_count}: injected pending-action reminder as a "
                f"SystemMessage at agent-turn {fired_at_turn} (agent_state.system_"
                f"messages updated; self.message unchanged)."
            )
            return self.message

        cloned = deepcopy(self.message)
        target = cloned
        if isinstance(target, MultiToolMessage):
            if not target.tool_messages:
                return self.message
            target = target.tool_messages[-1]
        if not hasattr(target, "content"):
            return self.message
        existing = target.content or ""
        target.content = f"{existing}\n\n{reminder_text}".strip()
        logger.debug(
            f"Step {self.step_count}: injected pending-action reminder into agent "
            f"input at agent-turn {fired_at_turn} (clone only; self.trajectory and "
            f"self.message are unaffected)."
        )
        return cloned

    def _pending_discoverable_tool_note(self) -> Optional[str]:
        """
        Domain-agnostic detector for the banking_knowledge discover->unlock->call
        ledger pattern (see FAILURE_ANALYSIS/.../failure_deepdive/02_discoverable_tool_ledger.md):
        a purely mechanical, observable-session-state signal -- NOT a judgment call
        and NOT derived from golden/expected actions. "Unlocked/given but never
        called" is computed identically regardless of which task is running or
        what the correct answer is, so unlike a task-classification-based
        reminder, it carries no leakage risk: the same code path runs whether or
        not the environment happens to expose this bookkeeping.

        Returns a note naming the specific unresolved tool(s), or None if this
        environment doesn't expose the ledger (non-banking domains, or a
        banking_knowledge session where nothing is currently pending). Only ever
        reads existing state; never calls, unlocks, or writes anything itself.
        """
        tools_obj = getattr(self.environment, "tools", None)
        if tools_obj is None:
            return None

        called_agent_tools: set[str] = set()
        called_user_tools: set[str] = set()
        for msg in self.trajectory:
            for tc in getattr(msg, "tool_calls", None) or []:
                if tc.name == "call_discoverable_agent_tool":
                    name = (tc.arguments or {}).get("agent_tool_name")
                    if name:
                        called_agent_tools.add(name)
                elif tc.name == "call_discoverable_user_tool":
                    name = (tc.arguments or {}).get("discoverable_tool_name")
                    if name:
                        called_user_tools.add(name)

        pending_agent: list[str] = []
        get_agent_state = getattr(tools_obj, "get_agent_discoverable_tools_state", None)
        if callable(get_agent_state):
            pending_agent = sorted(set(get_agent_state().keys()) - called_agent_tools)

        pending_user: list[str] = []
        get_user_state = getattr(tools_obj, "get_user_discoverable_tools_state", None)
        if callable(get_user_state):
            pending_user = sorted(set(get_user_state().keys()) - called_user_tools)

        if not pending_agent and not pending_user:
            return None

        parts = []
        if pending_agent:
            parts.append(
                "you unlocked but have not yet called: " + ", ".join(pending_agent)
            )
        if pending_user:
            parts.append(
                "you gave the customer but they have not yet called: "
                + ", ".join(pending_user)
            )
        return (
            "According to this session's own tool-call record, "
            + "; and ".join(parts)
            + ". If either is still needed to complete this request, resolve it now."
        )

    def _review_template(self) -> str:
        """The echo-back template for whichever checklist channel is active."""
        return (
            WORKFLOW_SKILL_REVIEW_TOOL_TEMPLATE
            if self.checklist_tool
            else WORKFLOW_SKILL_REVIEW_TEMPLATE
        )

    def _store_checklist(self, checklist: dict, agent_turn: int, source: str) -> None:
        """Common write path for both channels (text block and tool call).

        Under plan pinning the agent's own checklist is RECORDED but must not
        replace the pinned plan -- otherwise the plan stops being the fixed
        experimental variable and an agent that plans anyway silently converts
        its episode back into a control-arm episode.
        """
        skill = checklist["skill"]
        items = checklist["items"]
        if not self.pinned_plan:
            self._workflow_checklists = [
                c for c in self._workflow_checklists if c["skill"] != skill
            ] + [{"skill": skill, "items": items}]
        self._workflow_checklist_history.append(
            {
                "agent_turn": agent_turn,
                "skill": skill,
                "items": items,
                "n_items": len(items),
                "n_done": sum(1 for it in items if it["done"]),
                "source": source,
                "superseded_by_pinned_plan": bool(self.pinned_plan),
            }
        )

    def _parse_and_store_checklist_tool_calls(self, message: Message) -> None:
        """Record any `update_checklist` calls in this assistant turn.

        Runs at the same point in step() as the text-block parser -- i.e. BEFORE
        the tool call is dispatched -- so that `_enforce_checklist_compliance`
        sees the plan on the same turn it was written, exactly as it does for
        the text channel. Execution of the call (in _execute_tool_calls) then
        only has to produce the ack.
        """
        from tau2.orchestrator.checklist_tool import (
            CHECKLIST_TOOL_NAME,
            parse_checklist_call,
        )

        agent_turn = self._agent_turns_seen - 1
        for tc in getattr(message, "tool_calls", None) or []:
            if getattr(tc, "name", None) != CHECKLIST_TOOL_NAME:
                continue
            parsed = parse_checklist_call(tc.name, getattr(tc, "arguments", None))
            if parsed is None:
                # Recorded as an attempt so "called the tool but the arguments
                # were unusable" stays visible instead of looking like the model
                # never planned at all.
                self._checklist_tool_bad_calls += 1
                continue
            self._store_checklist(parsed, agent_turn, source="tool_call")

    def _parse_and_store_workflow_checklist(self, message: Message) -> None:
        """
        Scan for <workflow_checklist skill="...">...</workflow_checklist>
        blocks and store them as real orchestrator state in
        self._workflow_checklists, replacing any prior entry for the same skill
        name (a re-stated checklist is treated as the new authoritative snapshot
        for that skill, not merged item-by-item -- simpler, and matches how the
        agent is asked to rewrite the whole block each time in
        WORKFLOW_SKILL_REVIEW_TEMPLATE).

        The reminder instructs the agent to write this inside its thinking
        span, before it closes </think> and writes its actual reply --
        checked against `message.raw_data["thinking"]` first (populated by
        TAU2_WITH_TINKER/tinker_backend.py's generate_assistant, which already
        splits model output into `thinking` vs `content` at the </think>
        boundary; TinkerUser copies raw_data forward the same way). Also
        checks `message.content` as a second source and unions any matches
        found there -- models don't reliably follow "put X in your thinking"
        instructions (this whole project's own evidence says so repeatedly),
        so a checklist that leaks into the visible reply anyway still gets
        tracked, and _strip_workflow_checklist_for_delivery still cleans that
        copy before it reaches the user. Backends that don't populate
        raw_data["thinking"] (e.g. plain litellm-routed agents, whose raw_data
        is the raw provider response dict, not this shape) simply fall
        through to content-only parsing, same as before this method took a
        thinking source into account.

        Pure text parsing of what the agent itself already produced -- no new
        tool, no change to the environment/tools.py, nothing sent to or read
        from the grader. If the agent never uses this format, this is a
        silent no-op and _build_agent_input_message's REVIEW branch reports
        "no checklist recorded yet" rather than fabricating state that isn't
        there.

        This guarantees state is tracked *faithfully* (what's echoed back on a
        REVIEW turn is exactly what the harness last parsed, not a hope that the
        model recalls its own earlier text) -- it does NOT and cannot guarantee
        the checklist's *content* is correct. Verifying that the agent correctly
        recognized which skillset applies, or wrote a complete step list, would
        require comparing against the golden trajectory, which is leakage and is
        deliberately not done here.
        """
        sources = []
        thinking = (
            (message.raw_data or {}).get("thinking") if message.raw_data else None
        )
        if thinking:
            sources.append(("thinking", thinking))
        if message.content:
            sources.append(("content", message.content))
        agent_turn = self._agent_turns_seen - 1
        for source_name, text in sources:
            for match in _WORKFLOW_CHECKLIST_BLOCK_RE.finditer(text):
                # skill is optional now; unnamed checklists get a stable
                # placeholder so downstream keying still works.
                skill = (match.group(1) or "").strip() or "(unnamed)"
                items = []
                for line in match.group(2).splitlines():
                    item_match = _WORKFLOW_CHECKLIST_ITEM_RE.match(line.strip())
                    if item_match:
                        box = item_match.group(1)
                        items.append(
                            {
                                "text": item_match.group(2).strip(),
                                # Non-checkbox forms ("1." / "- ") cannot express
                                # done-ness; recorded as not-done, which is right
                                # for a plan the model has only just written.
                                "done": bool(box) and box.lower() == "x",
                            }
                        )
                if not items:
                    continue
                # Under plan pinning the agent's own block is RECORDED but must
                # not replace the pinned plan -- otherwise the plan stops being
                # the fixed experimental variable the arm depends on, and an
                # agent that writes its own checklist anyway silently converts
                # its episode back into a control-arm episode. The history entry
                # is still appended (tagged), because "did it try to write its
                # own plan even though one was handed to it, and did that plan
                # differ" is itself a finding worth having.
                self._store_checklist(
                    {"skill": skill, "items": items}, agent_turn, source=source_name
                )

    def _enforce_checklist_compliance(
        self, agent_input: Message, agent_msg: AssistantMessage
    ) -> AssistantMessage:
        """
        Mechanical, harness-level enforcement of "wrote a checklist when one
        was expected" -- not another prompt tweak. Measured before this
        existed: across Tier 1/2 (see orchestrator_reminder_scoping.md's
        2026-07-26 update), the CLASSIFY/REVIEW_NONE reminder fired 39 times
        with zero checklist ever established, and only 2 of those 39
        resulted in one being written. A wording change alone was judged not
        trustworthy enough given that history -- this forces a resample, the
        same class of mechanism TAU2_WITH_TINKER's tinker_backend.py already
        uses for duplicate-tool-call and empty-output cases, just triggered
        by a different, orchestrator-level signal (checklist state) instead
        of a backend-level one (raw completion shape).

        Only acts when self._checklist_enforcement_pending was set this turn
        by _build_agent_input_message (i.e. the CLASSIFY or REVIEW_NONE
        template fired -- a checklist is expected and none exists yet).
        Consumes (resets) the flag immediately so it can't leak into a later
        turn's decision.

        If _parse_and_store_workflow_checklist already found a checklist in
        agent_msg, compliant on the first try -- no retry, return unchanged.

        Otherwise, retries ONCE: rolls back the exact messages
        generate_next_message just appended to self.agent_state.messages
        (the input -- 1 entry, or len(tool_messages) for a MultiToolMessage
        input -- plus the 1 non-compliant reply), so the agent's own
        remembered history doesn't carry a draft that never reached the
        trajectory (same leakage concern _build_agent_input_message's own
        docstring documents for the reminder-mutation bug this project fixed
        2026-07-22 -- a wrong-but-different failure mode, same root risk:
        don't let a participant's own state.messages diverge from what
        actually happened). Then resamples with CHECKLIST_ENFORCEMENT_NUDGE
        appended to the same input. Whatever comes back (compliant or not)
        is final -- capped at one retry, same bound the existing
        duplicate-tool-call/empty-output retries use, so a model that still
        won't comply can't loop the episode.

        Every check (whether enforcement applied, whether it needed a retry,
        whether the retry worked) is appended to self._checklist_enforcement_log
        for persistence into SimulationRun.info -- see finalize().
        """
        if not self._checklist_enforcement_pending:
            return agent_msg
        self._checklist_enforcement_pending = False
        agent_turn = self._agent_turns_seen - 1  # already incremented for this turn
        if self._workflow_checklists:
            self._checklist_enforcement_log.append(
                {
                    "agent_turn": agent_turn,
                    "enforced": True,
                    "complied_first_try": True,
                    "retried": False,
                    "complied_after_retry": None,
                }
            )
            return agent_msg

        n_appended = (
            len(agent_input.tool_messages)
            if isinstance(agent_input, MultiToolMessage)
            else 1
        ) + 1
        if len(self.agent_state.messages) >= n_appended:
            del self.agent_state.messages[-n_appended:]

        # Deliver the nudge the same way pending_action_reminder_delivery says
        # the original reminder should go -- a real, confirmed bug had this
        # always content-appending regardless of setting, so a "system"-mode
        # run's retry was silently falling back to exactly the buried-after-
        # a-huge-tool-result position "system" mode exists to avoid, while
        # the run's *first* attempt (measured separately, via
        # _build_agent_input_message) correctly used SystemMessage delivery.
        # That mismatch was caught by comparing content-append occurrence
        # counts between message-mode and system-mode raw logs -- system-mode
        # logs showed the nudge landing in tool-result content anyway.
        # Its OWN marker, so it lands in a separate SystemMessage instead of
        # overwriting the CLASSIFY reminder -- which is what previously deleted
        # the format example this nudge tells the model to follow.
        nudge = (
            CHECKLIST_ENFORCEMENT_NUDGE_TOOL
            if self.checklist_tool
            else CHECKLIST_ENFORCEMENT_NUDGE
        )
        nudged = agent_input
        if not (
            self.pending_action_reminder_delivery == "system"
            and self._deliver_reminder_as_system_message(
                nudge, marker=_HARNESS_ENFORCEMENT_MARKER
            )
        ):
            nudged = deepcopy(agent_input)
            target = (
                nudged.tool_messages[-1]
                if isinstance(nudged, MultiToolMessage)
                else nudged
            )
            if hasattr(target, "content"):
                existing = target.content or ""
                target.content = f"{existing}\n\n{nudge}".strip()

        retried_msg, self.agent_state = self.agent.generate_next_message(
            nudged, self.agent_state
        )
        retried_msg.validate()
        self._parse_and_store_workflow_checklist(retried_msg)
        if self.checklist_tool:
            self._parse_and_store_checklist_tool_calls(retried_msg)
        complied = bool(self._workflow_checklists)
        logger.debug(
            f"Step {self.step_count}: checklist compliance retry at agent-turn "
            f"{agent_turn} -- complied_after_retry={complied}."
        )
        self._checklist_enforcement_log.append(
            {
                "agent_turn": agent_turn,
                "enforced": True,
                "complied_first_try": False,
                "retried": True,
                "complied_after_retry": complied,
            }
        )
        return retried_msg

    def _strip_workflow_checklist_for_delivery(self, message: Message) -> Message:
        """
        Return a clone of an agent message with any <workflow_checklist>...
        </workflow_checklist> block removed from its content, for delivery to
        the user (or any other recipient outside the agent itself).

        Defense-in-depth, not the primary mechanism: the reminder now
        instructs the agent to write the checklist inside its thinking span
        (see WORKFLOW_SKILL_CLASSIFY_TEMPLATE), which TAU2_WITH_TINKER's
        tinker_backend.py already splits from `content` at the </think>
        boundary before an AssistantMessage is even constructed -- so for a
        compliant model, `content` never contains the checklist in the first
        place, and this method is a no-op. It exists because a real, confirmed
        instance predates that fix: an agent turn whose entire *content* was
        nothing but a raw <workflow_checklist skill="..."> block with
        "- [ ]"/"- [x]" lines, sent to the simulated customer verbatim as a
        reply -- and this project's own repeated finding is that models don't
        reliably follow "put X in your thinking" instructions, so a model that
        still writes the block into its visible reply anyway should not leak
        it to the customer just because it didn't comply with the prompt.

        Only touches the copy handed to the recipient; self.trajectory and
        self.message keep the pristine, unmodified content the agent actually
        produced, same clone-before-modify pattern as
        _build_agent_input_message's pending-action-reminder injection. Tool-
        call messages are returned unchanged (tool calls route to Role.ENV, not
        Role.USER, so there's no content to leak there in the first place).

        If stripping the checklist would leave the content empty or
        whitespace-only, falls back to the original, unstripped message rather
        than delivering a blank turn. This should essentially never trigger in
        practice: TinkerBackend.generate_assistant() already refuses to hand
        the orchestrator a message with neither content nor a tool call --
        it resamples with a nudge, and substitutes a fallback reply if even
        that comes back empty -- so a real reply already exists in `content`
        before this method ever runs. Kept as a last-resort guard, not a
        mechanism this design relies on.
        """
        if isinstance(message, (ToolMessage, MultiToolMessage)):
            # ToolMessage/MultiToolMessage results are env-authored, not
            # agent-authored -- they can legitimately reach this method (this
            # branch handles both AGENT->USER and ENV->USER routing, e.g. a
            # ToolMessage answering a tool call the user-sim itself made,
            # routed back to the user). Neither type defines is_tool_call()
            # (that's only on ParticipantMessageBase subclasses, i.e.
            # AssistantMessage/UserMessage), and neither can ever contain a
            # <workflow_checklist> block in the first place, so skip straight
            # to returning unchanged rather than calling a method that isn't
            # there. Real, confirmed crash this fixes: AttributeError:
            # 'ToolMessage' object has no attribute 'is_tool_call'.
            return message
        if message.is_tool_call():
            return message
        content = getattr(message, "content", None)
        if not content or not _WORKFLOW_CHECKLIST_BLOCK_RE.search(content):
            return message
        stripped = _WORKFLOW_CHECKLIST_BLOCK_RE.sub("", content).strip()
        if not stripped:
            return message
        cloned = deepcopy(message)
        cloned.content = stripped
        return cloned

    def _format_workflow_checklists(self) -> str:
        """Render self._workflow_checklists back into the same
        <workflow_checklist> textual format the agent itself uses, so what's
        echoed back on a REVIEW turn is visually identical to what the agent
        would naturally write -- easier for it to directly edit/re-emit."""
        blocks = []
        for checklist in self._workflow_checklists:
            lines = [f'<workflow_checklist skill="{checklist["skill"]}">']
            for item in checklist["items"]:
                box = "[x]" if item["done"] else "[ ]"
                lines.append(f"- {box} {item['text']}")
            lines.append("</workflow_checklist>")
            blocks.append("\n".join(lines))
        return "\n".join(blocks)

    def step(self):
        """
        Perform one step of the simulation using half-duplex (turn-based) communication.

        Sends self.message from self.from_role to self.to_role.
        This can either be a message from agent to user/environment, environment to agent,
        or user to agent. Updates self.trajectory.
        """
        if self.done:
            raise ValueError("Simulation is done")
        logger.debug(
            f"Step {self.step_count}. Sending message from {self.from_role} to {self.to_role}"
        )
        logger.debug(
            f"Step {self.step_count}.\nFrom role: {self.from_role}\nTo role: {self.to_role}\nMessage: {self.message}"
        )
        # AGENT/ENV -> USER
        if self.from_role in [Role.AGENT, Role.ENV] and self.to_role == Role.USER:
            user_msg, self.user_state = self.user.generate_next_message(
                self._strip_workflow_checklist_for_delivery(self.message),
                self.user_state,
            )
            user_msg.validate()
            if UserSimulator.is_stop(user_msg):
                self.done = True
                self.termination_reason = TerminationReason.USER_STOP
            # Update voice metadata if audio was generated
            self._update_voice_metadata(user_msg)

            self.trajectory.append(user_msg)
            self.message = user_msg
            self.from_role = Role.USER
            if user_msg.is_tool_call():
                self.to_role = Role.ENV
            else:
                self.to_role = Role.AGENT
        # USER/ENV -> AGENT
        elif (
            self.from_role == Role.USER or self.from_role == Role.ENV
        ) and self.to_role == Role.AGENT:
            agent_input = self._build_agent_input_message()
            agent_msg, self.agent_state = self.agent.generate_next_message(
                agent_input, self.agent_state
            )
            agent_msg.validate()
            if self.pending_action_reminder_workflow_names or (
                self.pending_action_reminder_checklist_scope == "universal"
            ):
                # Both channels are parsed regardless of mode. The text parser
                # is a no-op when the model wrote none, and running it in tool
                # mode too means a model that ignores the tool and writes a
                # block anyway is still measured rather than scored as "never
                # planned" -- which would make the tool arm look worse for a
                # reason that has nothing to do with the tool.
                self._parse_and_store_workflow_checklist(agent_msg)
                if self.checklist_tool:
                    self._parse_and_store_checklist_tool_calls(agent_msg)
                agent_msg = self._enforce_checklist_compliance(agent_input, agent_msg)
            if self.agent.is_stop(agent_msg):
                self.done = True
                self.termination_reason = TerminationReason.AGENT_STOP

            self.trajectory.append(agent_msg)
            self.message = agent_msg
            self.from_role = Role.AGENT
            if agent_msg.is_tool_call():
                self.to_role = Role.ENV
            else:
                self.to_role = Role.USER
                # In solo mode, there is no user, so if the message is not a tool call and not a stop, then we end and report an agent error
                if self.solo_mode and not self.agent.is_stop(agent_msg):
                    self.done = True
                    self.termination_reason = TerminationReason.AGENT_ERROR
        # AGENT/USER -> ENV
        elif self.from_role in [Role.AGENT, Role.USER] and self.to_role == Role.ENV:
            if not self.message.is_tool_call():
                raise ValueError("Agent or User should send tool call to environment")
            tool_results = self._execute_tool_calls(self.message.tool_calls)
            assert len(self.message.tool_calls) == len(tool_results), (
                "Number of tool calls and tool messages should be the same"
            )
            self.trajectory.extend(tool_results)
            self.message = self._wrap_tool_results(tool_results)
            self.to_role = self.from_role
            self.from_role = Role.ENV
        else:
            raise ValueError(
                f"Invalid role combination. From role: {self.from_role}, To role: {self.to_role}"
            )
        if self.validate_communication:
            self.check_communication_error()
        self.step_count += 1
        self.environment.sync_tools()

    def get_trajectory(self) -> list[Message]:
        """
        Get the trajectory of the simulation.
        The trajectory is sorted by timestamp, turn_idx are added to messages, trajectory is returned.
        """
        messages: list[Message] = sorted(
            deepcopy(self.trajectory),
            key=lambda x: x.timestamp,
        )
        trajectory = []
        for i, msg in enumerate(messages):
            msg = deepcopy(msg)
            msg.turn_idx = i
            trajectory.append(msg)
        return trajectory

    def get_messages(self) -> list[Message]:
        """
        Get all messages from the simulation.

        For half-duplex mode, this is the same as get_trajectory().
        """
        return self.get_trajectory()

    @classmethod
    def validate_message_history(cls, message_history: list[Message]):
        """
        Validate a message history.
            - Should only contain AssistantMessage, UserMessage, ToolMessage
            - All assistant/user messages should be either to user or tool call, not both.
            - If n tool calls are made by a participant, exactly n tool messages should follow with requestor matching the participant.
        """
        num_expected_tool_messages = 0
        requestor = None
        for msg in message_history:
            if isinstance(msg, AssistantMessage) or isinstance(msg, UserMessage):
                msg.validate()
                if msg.is_tool_call():
                    if num_expected_tool_messages > 0:
                        raise ValueError(
                            f"{num_expected_tool_messages} tool messages are missing. Got {msg.role} message."
                        )
                    num_expected_tool_messages = len(msg.tool_calls)
                    requestor = msg.role
                else:
                    num_expected_tool_messages == 0
                    requestor = None
            elif isinstance(msg, ToolMessage):
                if num_expected_tool_messages == 0 or requestor is None:
                    raise ValueError("No tool messages expected.")
                if requestor != msg.requestor:
                    raise ValueError(
                        f"Got tool message from {msg.requestor}, expected {requestor}."
                    )
                num_expected_tool_messages -= 1
            else:
                raise ValueError(f"Invalid message type: {type(msg)}")

    def _count_errors(self, message_history: list[Message]) -> int:
        """
        Count the number of errors in the message history.
        """
        return sum(
            1 for msg in message_history if isinstance(msg, ToolMessage) and msg.error
        )

    def _add_timestamps(
        self, message_history: list[Message]
    ) -> list[tuple[str, Message]]:
        """
        Add timestamps to the message history.
        This is used to sort the messages by timestamp.
        """
        time_offset = datetime.now() - timedelta(seconds=len(message_history))
        for i, msg in enumerate(message_history):
            # Use ISO format (use_compact_format=False) to match get_now() default
            msg.timestamp = format_time(
                time_offset + timedelta(seconds=i), use_compact_format=False
            )
        return message_history

    def _update_voice_metadata(self, message: UserMessage) -> None:
        """
        Update voice metadata with simulation ID.
        Note: turn_idx is not available until get_trajectory() is called.
        """
        # Check if message has voice UUID (set during synthesis)
        if (
            hasattr(message, "_voice_uuid")
            and message.audio_path
            and self.simulation_id
        ):
            voice_uuid = message._voice_uuid
            audio_dir = Path(message.audio_path).parent
            metadata_path = audio_dir / "metadata.json"

            metadata = {
                "simulation_id": self.simulation_id,
                "timestamp": message.timestamp,
                "turn_uuid": voice_uuid,
            }

            with open(metadata_path, "w") as f:
                json.dump(metadata, f, indent=2)

    def _finalize_voice_metadata(self, messages: list[Message]) -> None:
        """
        Update all voice metadata files with final turn_idx values.
        """
        for msg in messages:
            if (
                isinstance(msg, UserMessage)
                and hasattr(msg, "_voice_uuid")
                and msg.audio_path
            ):
                audio_dir = Path(msg.audio_path).parent
                metadata_path = audio_dir / "metadata.json"

                if metadata_path.exists():
                    # Read existing metadata
                    with open(metadata_path, "r") as f:
                        metadata = json.load(f)

                    # Update with turn_idx
                    metadata["turn_idx"] = msg.turn_idx

                    # Write back
                    with open(metadata_path, "w") as f:
                        json.dump(metadata, f, indent=2)
