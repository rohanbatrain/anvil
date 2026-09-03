"""Health, and the decline taxonomy as a browsable, testable thing.

The classify endpoint exists because the single most important claim in the
architecture -- "rules first, the model only where rules genuinely fail" -- is
otherwise unfalsifiable prose. Here a reviewer can type ``A/c bal low`` and
watch it escalate, then type ``U30`` and watch it resolve with no model call.
"""

from __future__ import annotations

from fastapi import APIRouter

from anvil.api.schemas import (
    ClassifyRequest,
    ClassifyResult,
    FailureClassView,
    Health,
    TaxonomyView,
)
from anvil.api.state import get_state
from anvil.core.config import get_settings
from anvil.domain.enums import FailureClass
from anvil.domain.taxonomy import CODE_NAMESPACES, RETRY_CURVES, known_codes
from anvil.risk.classifier import classify_failure

router = APIRouter(tags=["system"])


@router.get("/health", response_model=Health)
async def health() -> Health:
    settings = get_settings()
    return Health(
        status="ok",
        mode=settings.mode.value,
        version="1.0.0",
        database="not required — the console runs from the seeded simulator",
        model=(
            "offline fixtures"
            if settings.is_offline
            else f"{settings.model_classifier} / {settings.model_planner}"
        ),
        seed=get_state().seed,
    )


@router.get("/api/taxonomy", response_model=TaxonomyView)
async def taxonomy() -> TaxonomyView:
    coverage = known_codes()
    classes: list[FailureClassView] = []
    for failure_class in FailureClass:
        curve = RETRY_CURVES[failure_class]
        examples: list[str] = []
        for table in CODE_NAMESPACES.values():
            examples.extend(code for code, mapped in table.items() if mapped is failure_class)
        classes.append(
            FailureClassView(
                failure_class=failure_class.value,
                posture=curve.posture.value,
                retryable=curve.is_retryable,
                max_attempts=curve.max_attempts,
                rationale=curve.rationale,
                example_codes=sorted(examples)[:8],
            )
        )
    return TaxonomyView(
        total_codes=sum(len(v) for v in coverage.values()),
        by_namespace={k: len(v) for k, v in coverage.items()},
        classes=classes,
    )


@router.post("/api/classify", response_model=ClassifyResult)
async def classify(request: ClassifyRequest) -> ClassifyResult:
    """Run the deterministic classifier and report whether it had to give up."""
    result = classify_failure(
        raw_code=request.raw_code,
        gateway_description=request.gateway_description,
        bank_narration=request.bank_narration,
        rail_hint=request.rail_hint,
    )
    if result.resolved:
        return ClassifyResult(
            resolved=True,
            failure_class=result.failure_class.value,  # type: ignore[union-attr]
            confidence_bps=result.confidence_bps,  # type: ignore[union-attr]
            matched_code=result.matched_code,  # type: ignore[union-attr]
            matched_namespace=result.matched_namespace,  # type: ignore[union-attr]
            escalation_reason=None,
            would_call_model=False,
            detail={"describes": result.describe()},  # type: ignore[union-attr]
        )
    return ClassifyResult(
        resolved=False,
        failure_class=None,
        confidence_bps=None,
        matched_code=None,
        matched_namespace=None,
        escalation_reason=result.reason,  # type: ignore[union-attr]
        would_call_model=True,
        detail={
            "candidates": [
                {"failure_class": fc.value, "confidence_bps": bps}
                for fc, bps in result.candidates  # type: ignore[union-attr]
            ]
        },
    )
