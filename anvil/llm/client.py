"""A real Claude client that satisfies ``ModelPort``.

This is the module that sits between the graph and Anthropic.  The prompts are
in ``anvil.llm.prompts``, the output contracts are in ``anvil.llm.schemas``,
and the redaction boundary is in ``anvil.llm.redaction``.  This module owns
only the wire call and the cost accounting.

Three things about this design are deliberate:

**Tool-use for structured output.**  Each call forces the model to respond
through a named tool whose ``input_schema`` is the JSON Schema of the
corresponding Pydantic model.  That means a response that does not parse is
never silently coerced -- it is a tool-call failure, caught here and raised
as a RuntimeError so the graph's documented degradation path handles it.

**Cost tracking in paise.**  Every call updates a running total so the case
can carry its own model cost, and the evidence report can subtract it from
the recovery it earned.  The per-token rates are estimates; production
billing would read the x-cost header when Anthropic exposes one.

**Raise, never return a fallback.**  A model error here is not caught and
papered over with a conservative guess.  The graph nodes in
``anvil.graph.nodes.reason`` already have their own fallback logic for when
a RuntimeError is raised, and duplicating that logic here would make the
degradation path untestable from outside.
"""

from __future__ import annotations

from typing import Any

import tenacity
from anthropic import AsyncAnthropic

from anvil.core.config import get_settings
from anvil.core.logging import get_logger
from anvil.llm.prompts import (
    DIAGNOSE_SYSTEM,
    build_compose,
    build_diagnose,
    build_plan,
    compose_system,
    plan_system,
)
from anvil.llm.schemas import OutreachDraft, RecoveryDiagnosis, RecoveryPlan

logger = get_logger(__name__)


class ClaudeModel:
    """Claude model integration satisfying ModelPort.

    This client wraps anthropic.AsyncAnthropic, enforces structured outputs
    via tool usage, and tracks cumulative costs across all calls in a case.
    Every method degrades on any network or parsing error, allowing the
    orchestrator to fall back to the deterministic paths.
    """

    def __init__(self) -> None:
        api_key = get_settings().anthropic_api_key.get_secret_value()
        self._client = AsyncAnthropic(api_key=api_key)
        self._cost_minor = 0
        self._model = "claude-sonnet-4-20250514"

    @property
    def cost_minor(self) -> int:
        """Cumulative spend across all calls for this case, in minor units (paise)."""
        return self._cost_minor

    def _track_cost(self, input_tokens: int, output_tokens: int) -> None:
        """Estimate cost based on token counts.

        Sonnet is roughly 2 paise per 1K input tokens, 10 paise per 1K output tokens.
        """
        self._cost_minor += (input_tokens * 2 // 1000) + (output_tokens * 10 // 1000)

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(2),
        wait=tenacity.wait_exponential(multiplier=1, min=1),
        reraise=True,
    )
    async def _diagnose_with_retry(self, context: dict[str, Any]) -> dict[str, Any]:
        tools = [
            {
                "name": "submit_diagnosis",
                "description": "Submit the diagnosis for the payment failure.",
                "input_schema": RecoveryDiagnosis.model_json_schema(),
            }
        ]

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=DIAGNOSE_SYSTEM,
            messages=[{"role": "user", "content": build_diagnose(context)}],
            tools=tools,
            tool_choice={"type": "tool", "name": "submit_diagnosis"},
        )

        self._track_cost(response.usage.input_tokens, response.usage.output_tokens)

        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_diagnosis":
                diagnosis = RecoveryDiagnosis.model_validate(block.input)
                posture = diagnosis.recommended_posture
                return {
                    "root_cause": diagnosis.root_cause,
                    "can_pay": diagnosis.can_pay,
                    "intends_to_pay": diagnosis.intends_to_pay,
                    "recommended_posture": posture.value if hasattr(posture, "value") else posture,
                    "confidence": diagnosis.confidence,
                    "source": "claude",
                }

        raise ValueError("Model did not return tool_use block.")

    async def diagnose(self, *, context: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._diagnose_with_retry(context)
        except Exception as e:
            logger.error("Claude diagnose failed", error=str(e))
            raise RuntimeError(f"diagnose failed: {e}") from e

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(2),
        wait=tenacity.wait_exponential(multiplier=1, min=1),
        reraise=True,
    )
    async def _plan_with_retry(
        self, context: dict[str, Any], allowed_actions: list[str], budget_minor: int
    ) -> dict[str, Any]:
        tools = [
            {
                "name": "submit_plan",
                "description": "Submit the recovery plan.",
                "input_schema": RecoveryPlan.model_json_schema(),
            }
        ]

        system_prompt = plan_system(allowed_actions, budget_minor)
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": build_plan(context, budget_minor=budget_minor)}],
            tools=tools,
            tool_choice={"type": "tool", "name": "submit_plan"},
        )

        self._track_cost(response.usage.input_tokens, response.usage.output_tokens)

        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_plan":
                plan = RecoveryPlan.model_validate(block.input)
                return {
                    "strategy": plan.strategy,
                    "steps": plan.model_dump(mode="json")["steps"],
                    "confidence": plan.confidence,
                }

        raise ValueError("Model did not return tool_use block.")

    async def plan(
        self, *, context: dict[str, Any], allowed_actions: list[str], budget_minor: int
    ) -> dict[str, Any]:
        try:
            return await self._plan_with_retry(context, allowed_actions, budget_minor)
        except Exception as e:
            logger.error("Claude plan failed", error=str(e))
            raise RuntimeError(f"plan failed: {e}") from e

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(2),
        wait=tenacity.wait_exponential(multiplier=1, min=1),
        reraise=True,
    )
    async def _compose_with_retry(
        self, context: dict[str, Any], purpose: str, language: str, allowed_facts: list[str]
    ) -> dict[str, Any]:
        tools = [
            {
                "name": "submit_draft",
                "description": "Submit the drafted message.",
                "input_schema": OutreachDraft.model_json_schema(),
            }
        ]

        system_prompt = compose_system(purpose, language, allowed_facts)
        user_msg = build_compose(context, purpose=purpose, language=language)
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
            tools=tools,
            tool_choice={"type": "tool", "name": "submit_draft"},
        )

        self._track_cost(response.usage.input_tokens, response.usage.output_tokens)

        for block in response.content:
            if block.type == "tool_use" and block.name == "submit_draft":
                draft = OutreachDraft.model_validate(block.input)
                return draft.model_dump(mode="json")

        raise ValueError("Model did not return tool_use block.")

    async def compose(
        self, *, context: dict[str, Any], purpose: str, language: str, allowed_facts: list[str]
    ) -> dict[str, Any]:
        try:
            return await self._compose_with_retry(context, purpose, language, allowed_facts)
        except Exception as e:
            logger.error("Claude compose failed", error=str(e))
            raise RuntimeError(f"compose failed: {e}") from e
