# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Select Operation Node - LLM picks which operation to call.

This is step 3 of the deterministic workflow.
The LLM reviews available operations and selects the best one.

Skill injection: If skill_content is available in state, it is prepended
to the prompt so the LLM has domain-specific knowledge when selecting
the most appropriate operation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meho_app.core.otel import get_logger
from meho_app.modules.agents.base.inference import infer_structured
from meho_app.modules.agents.specialist_agent.models import (
    NoRelevantOperation,
    OperationSelection,
)

if TYPE_CHECKING:
    from meho_app.modules.agents.specialist_agent.state import WorkflowState
    from meho_app.modules.agents.sse.emitter import EventEmitter

logger = get_logger(__name__)


@dataclass
class SelectOperationNode:
    """Node that asks LLM to select an operation.

    Skill content from state.skill_content is prepended to the prompt
    to provide domain-specific knowledge for better operation selection.

    Emits:
        thought: "Selecting best operation from results..."
        thought: "selected_operation: {operation_id}" or "no_relevant_operation: {reason}"
    """

    connector_name: str

    async def run(
        self,
        state: WorkflowState,
        emitter: EventEmitter | None,
        operations: list[dict[str, Any]],
    ) -> OperationSelection | NoRelevantOperation:
        """Execute the operation selection step.

        Args:
            state: Current workflow state (includes skill_content).
            emitter: Event emitter for SSE streaming.
            operations: List of operations from SearchOperationsNode.

        Returns:
            OperationSelection with chosen operation, or NoRelevantOperation.
        """
        # Emit thought event
        if emitter:
            await emitter.thought("Selecting best operation from results...")

        ops_text = "\n".join(
            [
                f"- {op.get('operation_id', op.get('name', 'unknown'))}: {op.get('description', 'No description')[:100]}"
                for op in operations
            ]
        )

        heuristic = self._select_operation_for_count(state.user_goal, operations)
        if heuristic:
            selection = OperationSelection(
                operation_id=heuristic["operation_id"],
                parameters={},
                reasoning=heuristic["reasoning"],
            )
            if emitter:
                await emitter.thought(
                    f"selected_operation: {selection.operation_id} (count heuristic)"
                )
            state.steps_executed.append(f"selected_operation: {selection.operation_id}")
            logger.debug(
                f"[{self.connector_name}] Selected via heuristic: {selection.operation_id}"
            )
            return selection

        # Prepend skill content for domain-specific knowledge
        skill_context = ""
        if state.skill_content:
            skill_context = f"""<domain_knowledge>
{state.skill_content}
</domain_knowledge>

"""

        prompt = f"""{skill_context}You are investigating connector "{self.connector_name}" to answer:

User Question: {state.user_goal}

Available operations:
{ops_text}

Select the operation that best answers the user's question.
If no operation is relevant, indicate that.

Selection guidance:
- If the question asks for counts, breakdowns, or "per X" aggregations, choose the operation that returns the *items to count*, not just the grouping keys.
- Prefer list/search operations that return full records (so SQL can filter/aggregate).
- If an operation can list all items without parameters, prefer that over listing only group names.
"""

        # Try to get OperationSelection first
        try:
            result = await infer_structured(
                prompt=prompt,
                response_model=OperationSelection,
            )
        except Exception:
            # Fall back to NoRelevantOperation
            result = await infer_structured(
                prompt=prompt + "\n\nExplain why no operation is relevant.",
                response_model=NoRelevantOperation,  # type: ignore[arg-type]
            )

        # Emit result
        if isinstance(result, NoRelevantOperation):
            if emitter:
                await emitter.thought(f"no_relevant_operation: {result.reasoning}")
            state.steps_executed.append(f"no_relevant_operation: {result.reasoning}")
        else:
            if emitter:
                await emitter.thought(f"selected_operation: {result.operation_id}")
            state.steps_executed.append(f"selected_operation: {result.operation_id}")

        logger.debug(
            f"[{self.connector_name}] Selected: "
            f"{result.operation_id if isinstance(result, OperationSelection) else 'none'}"
        )
        return result

    def _select_operation_for_count(  # NOSONAR (cognitive complexity)
        self,
        user_goal: str,
        operations: list[dict[str, Any]],
    ) -> dict[str, str] | None:
        target = self._extract_count_target(user_goal)
        if not target:
            return None
        group_term = self._extract_group_term(user_goal)
        variants = self._target_variants(target)
        if not variants:
            return None

        best_op: dict[str, Any] | None = None
        best_score = 0

        for op in operations:
            op_id = str(op.get("operation_id") or op.get("name") or "")
            description = str(op.get("description") or "")
            op_norm = self._normalize_text(op_id)
            desc_norm = self._normalize_text(description)

            has_target = any(v in op_norm or v in desc_norm for v in variants)
            if not has_target:
                continue

            score = 0
            for variant in variants:
                if variant in op_norm:
                    score += 5
                if variant in desc_norm:
                    score += 2

            if op_norm.startswith("list "):
                score += 2
            if op_norm.startswith("get all "):
                score += 2
            if op_norm.startswith("search "):
                score += 1
            if op_norm.startswith("get "):
                score += 1

            if group_term and group_term in op_norm and not any(v in op_norm for v in variants):
                score -= 3

            if score > best_score:
                best_score = score
                best_op = op

        if best_op and best_score >= 3:
            operation_id = str(best_op.get("operation_id") or best_op.get("name") or "")
            return {
                "operation_id": operation_id,
                "reasoning": (
                    f"Selected '{operation_id}' to count '{target}' items for aggregation."
                ),
            }

        return None

    def _extract_count_target(self, user_goal: str) -> str | None:  # NOSONAR (cognitive complexity)
        text = self._normalize_text(user_goal)
        tokens = text.split()
        if not tokens:
            return None
        stop_words = {
            "per",
            "by",
            "in",
            "for",
            "of",
            "on",
            "across",
            "within",
            "with",
            "without",
            "grouped",
            "group",
            "breakdown",
            "split",
            "each",
            "every",
            "from",
            "to",
            "and",
            "or",
            "where",
            "when",
            "which",
            "that",
        }

        def capture_after(phrase: list[str]) -> str | None:
            for i in range(len(tokens) - len(phrase) + 1):
                if tokens[i : i + len(phrase)] == phrase:
                    target_tokens = []
                    for token in tokens[i + len(phrase) :]:
                        if token in stop_words:
                            break
                        target_tokens.append(token)
                    if target_tokens:
                        return " ".join(target_tokens)
            return None

        for phrase in (["how", "many"], ["number", "of"], ["count", "of"]):
            target = capture_after(phrase)
            if target:
                return target

        for idx in range(1, len(tokens)):
            if tokens[idx] in {"count", "counts"} and tokens[idx - 1] not in stop_words:
                return tokens[idx - 1]

        for idx, token in enumerate(tokens):
            if token in {"count", "counts"} and idx + 1 < len(tokens):
                target_tokens = []
                for next_token in tokens[idx + 1 :]:
                    if next_token in stop_words:
                        break
                    target_tokens.append(next_token)
                if target_tokens:
                    return " ".join(target_tokens)

        return None

    def _extract_group_term(self, user_goal: str) -> str | None:
        text = self._normalize_text(user_goal)
        match = re.search(r"\b(?:per|by)\s+([a-z0-9]+)", text)
        if match:
            return match.group(1)
        return None

    def _target_variants(self, target: str) -> list[str]:
        normalized = self._normalize_text(target)
        if not normalized:
            return []
        variants = {normalized}
        tokens = normalized.split()
        if tokens:
            last = tokens[-1]
            if last.endswith("ies") and len(last) > 3:
                singular = last[:-3] + "y"
                variants.add(" ".join([*tokens[:-1], singular]))
            if last.endswith("s") and len(last) > 3:
                singular = last[:-1]
                variants.add(" ".join([*tokens[:-1], singular]))
            else:
                plural = last + "s"
                variants.add(" ".join([*tokens[:-1], plural]))
        return sorted(variants)

    def _normalize_text(self, text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
