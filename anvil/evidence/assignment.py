"""Which arm a case lands in, and the proof that nobody chose it afterwards.

A recovery number without a control arm is a press release. The whole point of
this module is that the arm a case ends up in is a *pure function of the batch
seed and the case id*, computed before any outcome is known, and recomputable by
anyone who doubts the result.

Three design choices carry that guarantee.

**The hash is the receipt.** :func:`assign` returns the full 256-bit digest it
used, not just the arm, and :class:`~anvil.db.models.experiment.ArmAssignment`
stores it. A judge who suspects the control arm was quietly pruned of its
successes can recompute every digest from the seed and the case ids alone and
diff the result against the table. Storing only the arm would make that
impossible; storing the digest makes tampering detectable rather than merely
discouraged.

**Buckets, not a coin flip.** Each case maps to one of 10000 buckets and the
split is expressed in basis points over the same 10000, so the arm boundaries
are exact integers. There is no floating-point threshold anywhere in the
assignment path, which means the boundary case is decided by ``<`` on two ints
rather than by whichever way a rounding error happened to fall.

**Arm order is fixed: control, then baseline, then anvil.** Control occupies the
lowest buckets. That ordering is not cosmetic -- it means growing the anvil arm
at the baseline arm's expense leaves *every control case exactly where it was*,
so two batches run at different splits still share a comparable holdout. Putting
the largest arm first would reshuffle the holdout on every split change and
quietly destroy that comparability.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from anvil.core.errors import ValidationError
from anvil.domain.enums import ExperimentArm

#: The assignment space. Matches the basis-point unit used for the split and for
#: every rate this package reports, so there is one denominator to reason about.
BUCKETS = 10_000

#: Fixed, and deliberately not alphabetical. See the module docstring: control
#: first is what keeps the holdout stable when the split changes.
ARM_ORDER: tuple[ExperimentArm, ...] = (
    ExperimentArm.CONTROL,
    ExperimentArm.BASELINE,
    ExperimentArm.ANVIL,
)


@dataclass(frozen=True, slots=True)
class ArmSplit:
    """How the population is divided, in basis points.

    Validated on construction rather than at assignment time. A split that does
    not sum to 10000 is not a recoverable condition -- it means some slice of the
    population has no arm at all -- so it is refused at the point where somebody
    could still fix it.
    """

    control_bps: int
    baseline_bps: int
    anvil_bps: int

    def __post_init__(self) -> None:
        for name, value in (
            ("control_bps", self.control_bps),
            ("baseline_bps", self.baseline_bps),
            ("anvil_bps", self.anvil_bps),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValidationError(f"{name} must be an int, got {type(value).__name__}")
            if value < 0:
                raise ValidationError(f"{name} cannot be negative", value=value)
        total = self.control_bps + self.baseline_bps + self.anvil_bps
        if total != BUCKETS:
            raise ValidationError(
                f"arm split must sum to {BUCKETS} basis points, got {total}; "
                "a split that does not sum to one leaves part of the population unassigned",
                control_bps=self.control_bps,
                baseline_bps=self.baseline_bps,
                anvil_bps=self.anvil_bps,
                total=total,
            )

    def share_bps(self, arm: ExperimentArm) -> int:
        """The requested share of one arm, in basis points."""
        if arm is ExperimentArm.CONTROL:
            return self.control_bps
        if arm is ExperimentArm.BASELINE:
            return self.baseline_bps
        return self.anvil_bps

    @property
    def boundaries(self) -> tuple[tuple[ExperimentArm, int, int], ...]:
        """Half-open bucket ranges per arm, in :data:`ARM_ORDER`.

        Exposed so a reviewer can read the partition off the object instead of
        inferring it from :meth:`arm_for_bucket`.
        """
        ranges: list[tuple[ExperimentArm, int, int]] = []
        lower = 0
        for arm in ARM_ORDER:
            upper = lower + self.share_bps(arm)
            ranges.append((arm, lower, upper))
            lower = upper
        return tuple(ranges)

    def arm_for_bucket(self, bucket: int) -> ExperimentArm:
        """Map a bucket onto its arm. Total over ``[0, BUCKETS)``."""
        if not isinstance(bucket, int) or isinstance(bucket, bool):
            raise ValidationError(f"bucket must be an int, got {type(bucket).__name__}")
        if not 0 <= bucket < BUCKETS:
            raise ValidationError(f"bucket {bucket} is outside [0, {BUCKETS})", bucket=bucket)
        for arm, lower, upper in self.boundaries:
            if lower <= bucket < upper:
                return arm
        # Unreachable while the split sums to BUCKETS, which __post_init__
        # guarantees. Kept so the function is total by construction.
        return ARM_ORDER[-1]

    def describe(self) -> str:
        """One line a human can check against the batch they thought they asked for."""
        return " / ".join(f"{arm.value} {self.share_bps(arm) / 100:.2f}%" for arm in ARM_ORDER)


#: Matches the column defaults on :class:`~anvil.db.models.experiment.RecoveryBatch`.
#: A tenth of the population is held out entirely: enough to measure the natural
#: self-cure rate, small enough that the holdout is not itself the cost centre.
DEFAULT_SPLIT = ArmSplit(control_bps=1_000, baseline_bps=1_000, anvil_bps=8_000)

#: An even three-way split, for calibration runs where the question is which arm
#: wins rather than how much money the agent recovered.
EVEN_SPLIT = ArmSplit(control_bps=3_334, baseline_bps=3_333, anvil_bps=3_333)


@dataclass(frozen=True, slots=True)
class Assignment:
    """One case's arm, with the evidence that put it there.

    ``assignment_hash`` and ``bucket`` are redundant with ``arm`` on purpose:
    together they let :func:`verify` reconstruct the decision from the seed
    alone, which is the difference between an auditable experiment and a claim.
    """

    case_id: str
    arm: ExperimentArm
    assignment_hash: str
    bucket: int


def assignment_hash(batch_seed: int, case_id: str) -> str:
    """The digest that decides the arm: BLAKE2b-256 over seed and case id.

    A cryptographic hash rather than :func:`hash` or a modulus of the id,
    because the property that matters is that the mapping cannot be *steered*.
    Anyone who could predict which ids land in control could select a
    flattering population; with BLAKE2b they would have to break the hash.

    The separator is ``\\x1f`` so that ``(seed=1, case="2x")`` and
    ``(seed=12, case="x")`` cannot collide by concatenation.
    """
    if not isinstance(batch_seed, int) or isinstance(batch_seed, bool):
        raise ValidationError(f"batch_seed must be an int, got {type(batch_seed).__name__}")
    if batch_seed <= 0:
        raise ValidationError("batch_seed must be positive so runs stay reproducible")
    if not case_id:
        raise ValidationError("case_id is required to assign an arm")
    payload = b"\x1f".join((str(batch_seed).encode(), case_id.encode()))
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def bucket_of(digest_hex: str) -> int:
    """Fold a digest into ``[0, BUCKETS)``.

    Plain modulo. The bias it introduces is bounded by ``BUCKETS / 2**256``,
    which is around 1e-73 -- many orders of magnitude below anything a batch of
    any conceivable size could detect. Rejection sampling would buy nothing and
    would make the mapping non-total, which is a real cost.
    """
    if len(digest_hex) != 64:
        raise ValidationError(
            f"expected a 64-character BLAKE2b-256 hex digest, got {len(digest_hex)} characters"
        )
    return int(digest_hex, 16) % BUCKETS


def assign(batch_seed: int, case_id: str, split: ArmSplit = DEFAULT_SPLIT) -> Assignment:
    """Assign one case to one arm, deterministically.

    Same seed and same case id always give the same arm, on any machine, in any
    order, however many other cases have been assigned first. That
    order-independence is what lets a batch be assembled incrementally, or
    re-assigned after a crash, without disturbing anything already decided.
    """
    digest = assignment_hash(batch_seed, case_id)
    bucket = bucket_of(digest)
    return Assignment(
        case_id=case_id,
        arm=split.arm_for_bucket(bucket),
        assignment_hash=digest,
        bucket=bucket,
    )


def assign_all(
    batch_seed: int, case_ids: Iterable[str], split: ArmSplit = DEFAULT_SPLIT
) -> tuple[Assignment, ...]:
    """Assign a whole population, preserving the input order.

    Duplicate ids are refused rather than silently collapsed: a case appearing
    twice in a batch would be counted twice in its arm's denominator, which is
    exactly the kind of quiet arithmetic error this package exists to prevent.
    """
    ids = list(case_ids)
    seen = set(ids)
    if len(seen) != len(ids):
        duplicates = sorted({cid for cid in ids if ids.count(cid) > 1})
        raise ValidationError(
            "a case may appear in a batch only once; duplicates would inflate an arm's denominator",
            duplicates=duplicates[:10],
        )
    return tuple(assign(batch_seed, case_id, split) for case_id in ids)


def verify(assignment: Assignment, batch_seed: int, split: ArmSplit = DEFAULT_SPLIT) -> bool:
    """Recompute a stored assignment from scratch and say whether it holds up.

    This is the function an auditor runs. It deliberately checks all three
    stored fields rather than just the arm, so an edited bucket or a swapped
    digest is caught even when the arm happens to still be consistent.
    """
    expected = assign(batch_seed, assignment.case_id, split)
    return (
        expected.assignment_hash == assignment.assignment_hash
        and expected.bucket == assignment.bucket
        and expected.arm is assignment.arm
    )


@dataclass(frozen=True, slots=True)
class RealisedSplit:
    """What the population actually looked like, against what was asked for.

    Requested and realised shares differ by sampling noise, and the gap is
    informative: a 10% holdout that came out at 6% on 200 cases is fine, while
    the same gap on 200000 cases means the assignment path is broken. Reporting
    the drift rather than only the request is what makes that distinguishable.
    """

    requested: ArmSplit
    counts: dict[ExperimentArm, int]
    total: int

    def count(self, arm: ExperimentArm) -> int:
        return self.counts.get(arm, 0)

    def realised_bps(self, arm: ExperimentArm) -> int:
        """Observed share, in basis points. Zero for an empty population."""
        if self.total <= 0:
            return 0
        return self.count(arm) * BUCKETS // self.total

    def drift_bps(self, arm: ExperimentArm) -> int:
        """Realised minus requested. Positive means the arm is over-represented."""
        return self.realised_bps(arm) - self.requested.share_bps(arm)

    @property
    def max_drift_bps(self) -> int:
        return max((abs(self.drift_bps(arm)) for arm in ARM_ORDER), default=0)

    @property
    def has_empty_arm(self) -> bool:
        """True when some arm the split asked for received nothing.

        Worth surfacing on its own: an empty control arm makes lift
        uncomputable, and an empty treatment arm makes the batch pointless.
        """
        return any(self.requested.share_bps(arm) > 0 and self.count(arm) == 0 for arm in ARM_ORDER)

    def describe(self) -> str:
        """One line per arm, requested against realised."""
        return " / ".join(
            f"{arm.value} {self.count(arm)} ({self.realised_bps(arm) / 100:.2f}%"
            f" vs {self.requested.share_bps(arm) / 100:.2f}% asked)"
            for arm in ARM_ORDER
        )


def realised_split(
    assignments: Sequence[Assignment], split: ArmSplit = DEFAULT_SPLIT
) -> RealisedSplit:
    """Tally a population's actual arm membership."""
    counts = dict.fromkeys(ARM_ORDER, 0)
    for assignment in assignments:
        counts[assignment.arm] = counts.get(assignment.arm, 0) + 1
    return RealisedSplit(requested=split, counts=counts, total=len(assignments))


def realised_split_from_counts(
    counts: dict[ExperimentArm, int], split: ArmSplit = DEFAULT_SPLIT
) -> RealisedSplit:
    """Same tally, when the caller already has counts rather than assignments.

    The metrics layer works from outcomes and never sees the assignment rows, so
    it needs this entry point to report drift without re-reading the batch.
    """
    filled = {arm: counts.get(arm, 0) for arm in ARM_ORDER}
    return RealisedSplit(requested=split, counts=filled, total=sum(filled.values()))
