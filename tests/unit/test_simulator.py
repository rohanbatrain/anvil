"""Tests for the seeded world.

The reproducibility tests are the important ones. "The same seed produces the
same batch" is a claim the submission makes in writing, and a claim like that is
worth exactly as much as the test that enforces it.

The distribution tests are the second line of defence, against a subtler
failure: a simulator that is reproducible but wrong. If the issuer's failure mix
or its settle rate drifts away from the ranges real payment data occupies, every
number the batch reports becomes fiction -- reproducible fiction.
"""

from __future__ import annotations

import collections
import datetime as dt
from decimal import Decimal

import pytest
from anvil.domain.enums import AuthorisationType, ExperimentArm, FailureClass
from anvil.domain.taxonomy import classify_code
from anvil.simulator.issuer import (
    BANKS,
    UNMAPPED_CODE_SHARE,
    DebitRequest,
    Issuer,
)
from anvil.simulator.population import build_population
from anvil.simulator.world import World

NOW = dt.datetime(2026, 9, 1, 6, 0, tzinfo=dt.UTC)
SEED = 20260902


def request(
    *,
    ability: str = "0.75",
    day: int = 12,
    hour: int = 11,
    attempts: int = 0,
    bank_index: int = 0,
    **flags: bool,
) -> DebitRequest:
    return DebitRequest(
        attempt_key=f"k{day}{hour}{ability}{attempts}",
        at=dt.datetime(2026, 9, day, hour, tzinfo=dt.UTC),
        amount_minor=1_499_00,
        bank=BANKS[bank_index],
        auth_type=AuthorisationType.UPI_AUTOPAY,
        ability_to_pay=Decimal(ability),
        attempts_this_cycle=attempts,
        **flags,
    )


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


def test_the_same_seed_builds_the_same_population() -> None:
    a = build_population(seed=SEED, size=400, now=NOW)
    b = build_population(seed=SEED, size=400, now=NOW)
    assert a.fingerprint() == b.fingerprint()
    assert a.merchant_id == b.merchant_id
    assert a.total_mrr == b.total_mrr


def test_a_different_seed_builds_a_different_population() -> None:
    a = build_population(seed=SEED, size=400, now=NOW)
    b = build_population(seed=SEED + 1, size=400, now=NOW)
    assert a.fingerprint() != b.fingerprint()


def test_the_issuer_is_deterministic() -> None:
    first = Issuer(SEED).present(request())
    second = Issuer(SEED).present(request())
    assert first == second


def test_the_same_seed_produces_the_same_at_risk_set() -> None:
    """The batch's population of cases must be reproducible, not just the book."""
    population = build_population(seed=SEED, size=500, now=NOW)
    first = World(population).open_cases()
    second = World(population).open_cases()
    assert [c.case_id for c in first] == [c.case_id for c in second]
    assert [c.true_failure_class for c in first] == [c.true_failure_class for c in second]
    assert [c.arm for c in first] == [c.arm for c in second]


def test_a_whole_batch_is_reproducible() -> None:
    population = build_population(seed=SEED, size=300, now=NOW)
    first = [(o.case_id, o.recovered_minor, o.status) for o in World(population).run_batch()]
    second = [(o.case_id, o.recovered_minor, o.status) for o in World(population).run_batch()]
    assert first == second


# ---------------------------------------------------------------------------
# The issuer is calibrated to something real
# ---------------------------------------------------------------------------


def test_a_healthy_debit_clears_at_a_realistic_rate() -> None:
    """Real subscription debit success sits in the high eighties to low nineties."""
    probability = Issuer(SEED).settle_probability(request(ability="0.80"))
    assert Decimal("0.85") <= probability <= Decimal("0.96"), probability


def test_the_overnight_maintenance_window_is_material() -> None:
    """Retrying at 02:00 IST must be visibly worse than retrying at 16:00."""
    issuer = Issuer(SEED)
    business = issuer.settle_probability(request(hour=11))
    overnight = issuer.settle_probability(request(hour=21))  # 02:30 IST
    assert overnight < business / 2, (overnight, business)


def test_the_salary_cycle_moves_a_stretched_customer() -> None:
    """The signal the scheduler exists to exploit. Without it the module is theatre."""
    issuer = Issuer(SEED)
    thin = issuer.settle_probability(request(ability="0.35", day=20))
    payday = issuer.settle_probability(request(ability="0.35", day=30))
    assert payday - thin > Decimal("0.08"), (thin, payday)


def test_a_comfortable_customer_barely_notices_the_cycle() -> None:
    """The cycle must bite the thin end, not everyone equally."""
    issuer = Issuer(SEED)
    thin = issuer.settle_probability(request(ability="0.95", day=20))
    payday = issuer.settle_probability(request(ability="0.95", day=30))
    assert abs(payday - thin) < Decimal("0.08")


@pytest.mark.parametrize(
    "flag",
    ["mandate_revoked", "account_closed", "instrument_expired", "mandate_paused"],
)
def test_terminal_conditions_never_settle(flag: str) -> None:
    """A revoked mandate does not settle with 3% probability. It does not settle."""
    issuer = Issuer(SEED)
    assert issuer.settle_probability(request(**{flag: True})) == Decimal(0)
    assert not issuer.present(request(**{flag: True})).settled


def test_repeat_presentments_get_harder() -> None:
    issuer = Issuer(SEED)
    rates = [issuer.settle_probability(request(attempts=n)) for n in range(4)]
    assert rates == sorted(rates, reverse=True)


def test_banks_differ_consistently() -> None:
    """A weak issuer must be weak every time, not randomly weak."""
    issuer = Issuer(SEED)
    strong = issuer.settle_probability(request(bank_index=0))
    weak = issuer.settle_probability(request(bank_index=7))
    assert weak < strong
    assert issuer.settle_probability(request(bank_index=7)) == weak


def test_a_customer_keeps_their_bank() -> None:
    issuer = Issuer(SEED)
    assert issuer.pick_bank("cus_1") == issuer.pick_bank("cus_1")


# ---------------------------------------------------------------------------
# The reason strings give the classifier real work
# ---------------------------------------------------------------------------


def test_roughly_a_fifth_of_reason_codes_are_unmapped() -> None:
    """The share that justifies having an LLM classifier at all."""
    issuer = Issuer(SEED)
    failures = [
        outcome
        for i in range(4000)
        if not (
            outcome := issuer.present(
                request(ability="0.45", day=(i % 28) + 1, hour=(i * 7) % 24, attempts=i % 3)
            )
        ).settled
    ]
    assert len(failures) > 500, "need enough failures to measure the share"
    unmapped = sum(1 for f in failures if f.code_is_unmapped)
    share = Decimal(unmapped) / Decimal(len(failures))
    assert abs(share - UNMAPPED_CODE_SHARE) < Decimal("0.06"), share


def test_unmapped_codes_really_do_defeat_the_tables() -> None:
    """A code the simulator calls unmapped must not quietly resolve."""
    issuer = Issuer(SEED)
    checked = 0
    for i in range(3000):
        outcome = issuer.present(request(ability="0.35", day=(i % 28) + 1, attempts=i % 3))
        if outcome.settled or not outcome.code_is_unmapped:
            continue
        checked += 1
        assert classify_code(outcome.raw_code or "") is None, outcome.raw_code
    assert checked > 20, "expected some unmapped codes in the sample"


def test_mapped_codes_do_resolve() -> None:
    issuer = Issuer(SEED)
    checked = 0
    for i in range(2000):
        outcome = issuer.present(request(ability="0.35", day=(i % 28) + 1, attempts=i % 3))
        if outcome.settled or outcome.code_is_unmapped:
            continue
        if classify_code(outcome.raw_code or "") is not None:
            checked += 1
    assert checked > 20


def test_the_failure_mix_is_dominated_by_recoverable_causes() -> None:
    """A book where most failures are terminal is not a recovery problem."""
    issuer = Issuer(SEED)
    mix: collections.Counter[FailureClass] = collections.Counter()
    for i in range(3000):
        outcome = issuer.present(request(ability="0.45", day=(i % 28) + 1, hour=(i * 5) % 24))
        if not outcome.settled and outcome.true_failure_class:
            mix[outcome.true_failure_class] += 1
    total = sum(mix.values())
    recoverable = mix[FailureClass.INSUFFICIENT_FUNDS] + mix[FailureClass.ISSUER_TECHNICAL]
    assert recoverable / total > 0.6, mix


# ---------------------------------------------------------------------------
# The population is a plausible book
# ---------------------------------------------------------------------------


def test_the_monthly_failure_rate_is_realistic() -> None:
    """Between roughly one in twenty and one in four, which is the real range."""
    population = build_population(seed=SEED, size=2000, now=NOW)
    cases = World(population).open_cases()
    rate = len(cases) / len(population.customers)
    assert 0.05 <= rate <= 0.25, rate


def test_most_subscribers_can_afford_their_subscription() -> None:
    population = build_population(seed=SEED, size=1000, now=NOW)
    comfortable = sum(1 for c in population.customers if c.traits.ability_to_pay > Decimal("0.5"))
    assert comfortable / len(population.customers) > 0.75


def test_the_mandate_mix_includes_the_interesting_cases() -> None:
    """Without Reserve Pay blocks and delegated authority the step-up path never runs."""
    population = build_population(seed=SEED, size=1500, now=NOW)
    kinds = collections.Counter(c.authorisation.auth_type for c in population.customers)
    assert kinds[AuthorisationType.RESERVE_PAY] > 0
    assert kinds[AuthorisationType.DELEGATED_AGENT] > 0
    assert kinds[AuthorisationType.UPI_AUTOPAY] > kinds[AuthorisationType.CARD_MANDATE]


def test_plans_form_a_ladder_so_a_downgrade_has_somewhere_to_go() -> None:
    population = build_population(seed=SEED, size=100, now=NOW)
    amounts = [p.amount.minor for p in population.plans]
    assert amounts == sorted(amounts)
    assert len(set(amounts)) == len(amounts)


def test_no_placeholder_names() -> None:
    """Nothing in a demo should read as John Doe."""
    population = build_population(seed=SEED, size=200, now=NOW)
    names = {c.display_name for c in population.customers}
    assert not any("Doe" in n or "Acme" in n or "Lorem" in n for n in names)
    assert len(names) > 20


# ---------------------------------------------------------------------------
# The arms behave as their definitions require
# ---------------------------------------------------------------------------


def test_the_control_arm_never_attempts_anything() -> None:
    """If control acts, it is not a control."""
    population = build_population(seed=SEED, size=600, now=NOW)
    world = World(population)
    for outcome in world.run_batch():
        if outcome.arm is ExperimentArm.CONTROL:
            assert outcome.attempts == 0
            assert outcome.contacts == 0
            assert outcome.concession_minor == 0


def test_the_control_arm_still_recovers_something() -> None:
    """Self-cure is real, and it is why an uncontrolled number overstates itself."""
    population = build_population(seed=SEED, size=900, now=NOW)
    control = [o for o in World(population).run_batch() if o.arm is ExperimentArm.CONTROL]
    assert control
    assert any(o.recovered for o in control)
    assert not all(o.recovered for o in control)


def test_the_baseline_retries_blindly_including_terminal_cases() -> None:
    """The flaw the baseline exists to demonstrate."""
    population = build_population(seed=SEED, size=1200, now=NOW)
    outcomes = World(population).run_batch()
    terminal = [
        o
        for o in outcomes
        if o.arm is ExperimentArm.BASELINE
        and o.true_failure_class is FailureClass.INSTRUMENT_EXPIRED
    ]
    if terminal:
        assert any(o.attempts > 0 for o in terminal)


def test_anvil_refuses_to_retry_a_decline_it_recognised() -> None:
    """Retrying a risk decline damages the merchant's issuer standing.

    The guarantee is stated over the *observed* class rather than the true one,
    and the distinction is real: a risk decline that arrives as free text with
    the model unavailable is classified UNKNOWN, and Anvil will spend the one
    conservative attempt that class permits. It cannot refuse what it was never
    able to identify. What it must never do is recognise a risk decline and
    retry it anyway -- that is a policy failure rather than an information gap.
    """
    population = build_population(seed=SEED, size=1500, now=NOW)
    outcomes = World(population).run_batch()
    recognised = [
        o
        for o in outcomes
        if o.arm is ExperimentArm.ANVIL and o.observed_failure_class is FailureClass.RISK_DECLINED
    ]
    assert recognised, "expected some recognised risk declines in this sample"
    for outcome in recognised:
        assert outcome.attempts == 0, outcome.case_id


def test_an_unrecognised_decline_costs_at_most_one_attempt() -> None:
    """The price of not knowing, bounded by design.

    UNKNOWN permits a single attempt precisely so that an unclassifiable failure
    cannot turn into a retry storm against an issuer that has already said no.
    """
    population = build_population(seed=SEED, size=1500, now=NOW)
    outcomes = World(population).run_batch()
    unknown = [
        o
        for o in outcomes
        if o.arm is ExperimentArm.ANVIL and o.observed_failure_class is FailureClass.UNKNOWN
    ]
    assert unknown, "expected some unclassifiable failures in this sample"
    for outcome in unknown:
        assert outcome.attempts <= 1, (outcome.case_id, outcome.attempts)


def test_a_batch_completes_quickly() -> None:
    """A thirty-day horizon over a real book must run in seconds, not minutes."""
    import time

    population = build_population(seed=SEED, size=800, now=NOW)
    started = time.monotonic()
    World(population).run_batch()
    assert time.monotonic() - started < 20.0
