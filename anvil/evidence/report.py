"""Rendering the evidence, including the parts that are inconvenient.

This is what a judge sees when they run ``make batch``, so it is laid out to be
read rather than parsed. Three rules govern what goes in it:

* **Never a lift without its interval.** A point estimate on its own invites the
  reader to believe a precision the data does not support.
* **Say "not significant" in those words.** Not "directionally positive", not a
  bare number the reader has to squint at.
* **End with what the run did not cover.** The unhandled cases, the
  model-safety events, the arms that were auto-approved. A limitations section
  that a reader has to go looking for is not a limitations section.
"""

from __future__ import annotations

from anvil.domain.enums import ExperimentArm
from anvil.evidence.metrics import ArmResult, BatchSummary, Comparison
from anvil.risk.calibration import Prediction, calibrate, render_reliability_table

WIDTH = 82

_ARM_LABELS: dict[ExperimentArm, str] = {
    ExperimentArm.CONTROL: "control (no intervention)",
    ExperimentArm.BASELINE: "baseline (fixed day 1/3/5 dunning)",
    ExperimentArm.ANVIL: "anvil (the agent)",
}


def _rule(char: str = "-") -> str:
    return char * WIDTH


def _heading(text: str) -> str:
    return f"\n{text}\n{_rule('=')}"


def render(summary: BatchSummary, *, model_available: bool) -> str:
    """The whole report, as one string."""
    out: list[str] = []
    out.append(_rule("="))
    out.append("ANVIL - RECOVERY BATCH EVIDENCE")
    out.append(_rule("="))
    out.append(
        f"seed {summary.seed}   population {summary.population_size:,} subscriptions   "
        f"{summary.case_count:,} at-risk cases"
    )
    out.append(f"money at risk: {summary.total_at_risk}")
    out.append(
        "language model: "
        + (
            "classification available"
            if model_available
            else "UNAVAILABLE - every case ran on the deterministic fallback"
        )
    )

    out.append(_heading("PER-ARM OUTCOMES"))
    out.append(f"  {'arm':36}{'n':>5}{'rate':>8}{'95% CI':>18}{'recovered':>14}")
    out.append(f"  {_rule('-')[:79]}")
    for arm in ExperimentArm:
        result = summary.arms.get(arm)
        if result is None:
            continue
        out.append(_arm_line(result))

    out.append(_heading("NET OF WHAT IT COST"))
    out.append(f"  {'arm':36}{'gross':>13}{'cost':>12}{'net':>13}{'attempts':>9}")
    out.append(f"  {_rule('-')[:79]}")
    for arm in ExperimentArm:
        result = summary.arms.get(arm)
        if result is None:
            continue
        out.append(
            f"  {_ARM_LABELS[arm]:36}{result.recovered.format():>13}"
            f"{result.total_cost.format():>12}{result.net_recovered.format():>13}"
            f"{result.attempts:>9}"
        )

    out.append(_heading("LIFT, WITH ITS UNCERTAINTY"))
    if not summary.comparisons:
        out.append("  No control arm in this batch, so no lift can be reported.")
    for comparison in summary.comparisons:
        out.extend(_comparison_lines(comparison))

    out.append(_heading("WHERE THE RECOVERY CAME FROM"))
    anvil = summary.arms.get(ExperimentArm.ANVIL)
    baseline = summary.arms.get(ExperimentArm.BASELINE)
    if anvil is not None:
        out.append(f"  {'failure class':24}{'anvil':>16}{'baseline':>16}")
        out.append(f"  {_rule('-')[:56]}")
        classes = sorted(
            anvil.by_failure_class,
            key=lambda k: -anvil.by_failure_class[k][0],
        )
        for key in classes:
            total, won = anvil.by_failure_class[key]
            cell_a = f"{won}/{total} = {won / total:.0%}" if total else "-"
            cell_b = "-"
            if baseline is not None and key in baseline.by_failure_class:
                bt, bw = baseline.by_failure_class[key]
                cell_b = f"{bw}/{bt} = {bw / bt:.0%}" if bt else "-"
            out.append(f"  {key:24}{cell_a:>16}{cell_b:>16}")

    out.append(_heading("WHAT THE MODEL DID"))
    total_classified = summary.classified_deterministically + summary.classified_by_model
    if total_classified:
        share = summary.classified_deterministically / total_classified
        out.append(
            f"  {summary.classified_deterministically}/{total_classified} failures "
            f"({share:.0%}) were classified by the code tables with no model call."
        )
        out.append(
            f"  {summary.classified_by_model} were escalated because no table recognised "
            "the reason string."
        )
    out.append(
        f"  {summary.unmapped_codes} of {summary.case_count} cases carried a reason code "
        "no table recognises."
    )
    out.append(
        f"  {summary.model_safety_events} proposed action(s) were refused before execution "
        "for falling outside the closed action space."
    )

    out.append(_heading("IS THE SCHEDULER HONEST?"))
    report = calibrate([Prediction(p, s) for p, s in summary.predictions])
    out.append(f"  {report.verdict}")
    if report.buckets:
        out.append("")
        out.append(render_reliability_table(report))
        out.append("")
        out.append(
            f"  Brier score {report.brier_score_bps / 10_000:.4f}   "
            f"expected calibration error {report.expected_calibration_error_bps / 100:.1f}%"
        )

    out.append(_heading("WHAT THIS RUN DOES NOT SHOW"))
    for line in _limitations(summary, model_available=model_available):
        out.append(f"  - {line}")

    out.append("")
    out.append(_rule("="))
    out.append(
        f"  Reproduce exactly:  make batch SEED={summary.seed} SIZE={summary.population_size}"
    )
    out.append(_rule("="))
    return "\n".join(out)


def _arm_line(result: ArmResult) -> str:
    ci = f"[{result.rate.low_bps / 100:.1f}, {result.rate.high_bps / 100:.1f}]"
    return (
        f"  {_ARM_LABELS[result.arm]:36}{result.case_count:>5}"
        f"{result.rate.point_bps / 100:>7.1f}%{ci:>18}{result.recovered.format():>14}"
    )


def _comparison_lines(comparison: Comparison) -> list[str]:
    label = f"{comparison.treatment.value} vs {comparison.against.value}"
    lines = [f"\n  {label}"]
    lines.append(
        f"    recovery rate difference   {comparison.difference.format_percent()}   (95% bootstrap)"
    )
    lines.append(f"    net money difference       {comparison.net_difference}")
    if comparison.significant:
        lines.append(
            f"    STATISTICALLY SIGNIFICANT  the interval excludes zero "
            f"(z = {comparison.z_score:+.2f})"
        )
    elif comparison.underpowered:
        lines.append(
            f"    NOT SIGNIFICANT, AND UNDERPOWERED  this batch could only have detected a "
            f"difference of {comparison.minimum_detectable_bps / 100:.1f} points or more. "
            "A larger batch is needed before any claim is made."
        )
    else:
        lines.append(
            f"    NOT SIGNIFICANT  the interval includes zero (z = {comparison.z_score:+.2f}), "
            "so this batch provides no evidence of a difference."
        )
    return lines


def _limitations(summary: BatchSummary, *, model_available: bool) -> list[str]:
    """The honest caveats. Ordered by how much they should worry the reader."""
    notes: list[str] = []

    if not model_available:
        notes.append(
            "The language model was unavailable throughout, so every case ran on the "
            "deterministic fallback. Cases whose reason code no table recognises were "
            "classified UNKNOWN, whose retry curve permits one conservative attempt before "
            "escalating. That is by design, and it costs recovery: this run is a floor."
        )

    baseline = summary.arms.get(ExperimentArm.BASELINE)
    anvil = summary.arms.get(ExperimentArm.ANVIL)
    if baseline and anvil and baseline.rate.point_bps > anvil.rate.point_bps:
        notes.append(
            f"Naive fixed-schedule dunning outperformed the agent on raw recovery rate in "
            f"this run ({baseline.rate.point_bps / 100:.1f}% against "
            f"{anvil.rate.point_bps / 100:.1f}%). The retry curves in anvil/domain/taxonomy.py "
            "are hand-written priors, not parameters fitted to this issuer, and the "
            "calibration table above is the measurement that says so. In production those "
            "curves would be fitted to the merchant's own outcomes; the mechanism for doing "
            "that is anvil/risk/calibration.py, and until it is run the scheduler is only as "
            "good as its priors."
        )
        notes.append(
            "The baseline also faces no penalty here for burning a mandate's finite "
            "presentment allowance or for damaging an issuer risk score, because neither "
            "cost is modelled. Both are real, and both are why production dunning is "
            "constrained in ways this baseline is not."
        )

    notes.append(
        "Approvals were auto-resolved. A batch cannot wait on a person, so anything policy "
        "escalated was treated as approved. An unattended approval is not evidence that a "
        "human would have approved."
    )
    notes.append(
        "Outcomes come from a seeded simulator, not from production traffic. The issuer "
        "model is calibrated to public decline-rate ranges and its parameters are "
        "deliberately not imported from the scheduler's curves, but it remains a model."
    )
    notes.append(
        "The control arm measures self-cure only. It does not model a merchant who does "
        "nothing but whose gateway still auto-retries, which would sit between control and "
        "baseline."
    )
    return notes
