"""The ledger's invariants, tested as properties rather than as examples.

Section 6 of ``docs/ARCHITECTURE.md`` numbers ten invariants. Four of them are
the ledger's, and each has a test here marked ``@pytest.mark.invariant``. They
are properties, not examples, because "this particular posting balances" is a
much weaker claim than "no posting this module can construct fails to balance",
and the second is the one the submission actually rests on.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from anvil.core.errors import UnbalancedTransaction, ValidationError
from anvil.domain.enums import EntryDirection, LedgerTxnType
from anvil.domain.money import Currency, Money, sum_money
from anvil.ledger.accounts import CHART, AccountCode, ChartOfAccounts, normal_direction
from anvil.ledger.posting import (
    EntryDraft,
    PostingContext,
    TransactionDraft,
    credit,
    debit,
    fund_budget,
    grant_concession,
    recognise_receivable,
    record_channel_cost,
    record_model_cost,
    reverse_draft,
    settle_recovered_debit,
    validate,
    write_off,
)
from anvil.ledger.reservations import (
    BudgetPosition,
    ReservationRequest,
    check_caps,
)
from hypothesis import assume, given, settings
from hypothesis import strategies as st

MERCHANT = "mch_01TESTMERCHANT00000000000"
CUSTOMER = "cus_01TESTCUSTOMER00000000000"
CASE = "cse_01TESTCASE0000000000000000"
AT = dt.datetime(2026, 9, 2, 11, 30, tzinfo=dt.UTC)

CHART_REF = ChartOfAccounts.derive(MERCHANT, customer_ids=(CUSTOMER,))


def ctx(**overrides: object) -> PostingContext:
    base = {
        "chart": CHART_REF,
        "effective_at": AT,
        "case_id": CASE,
        "customer_id": CUSTOMER,
    }
    base.update(overrides)
    return PostingContext(**base)  # type: ignore[arg-type]


money = st.builds(Money, st.integers(min_value=1, max_value=50_000_000), st.just(Currency.INR))

ALL_BUILDERS = [
    ("recognise_receivable", lambda c, m: recognise_receivable(c, m)),
    ("settle_recovered_debit", lambda c, m: settle_recovered_debit(c, m)),
    ("fund_budget", lambda c, m: fund_budget(c, m)),
    ("grant_concession", lambda c, m: grant_concession(c, m)),
    ("record_channel_cost", lambda c, m: record_channel_cost(c, m, "whatsapp")),
    ("record_model_cost", lambda c, m: record_model_cost(c, m, "claude-opus-5")),
    ("write_off", lambda c, m: write_off(c, m, "mandate revoked")),
]


# ---------------------------------------------------------------------------
# Invariant 2: every transaction balances
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@given(amount=money)
@settings(max_examples=200, deadline=None)
def test_every_builder_produces_a_balanced_transaction(amount: Money) -> None:
    """Invariant 2, over every builder and every amount.

    This is the single most important test in the repository. If it can be made
    to fail, money can be created or destroyed.
    """
    for name, build in ALL_BUILDERS:
        draft = build(ctx(), amount)
        assert draft.imbalance_minor == 0, f"{name} did not balance at {amount}"
        assert draft.total_debits == draft.total_credits, name
        assert draft.total_debits.is_positive, name


@pytest.mark.invariant
def test_unbalanced_transaction_is_refused() -> None:
    """A hand-built imbalance is caught before anything is written."""
    cash = CHART_REF.ref(AccountCode.MERCHANT_CASH)
    revenue = CHART_REF.ref(AccountCode.MERCHANT_REVENUE)
    draft = TransactionDraft(
        merchant_id=MERCHANT,
        txn_type=LedgerTxnType.MANDATE_DEBIT_SETTLED,
        currency=Currency.INR,
        effective_at=AT,
        narration="deliberately wrong",
        idempotency_key="k1",
        entries=(debit(cash, Money(100_00)), credit(revenue, Money(99_00))),
    )
    with pytest.raises(UnbalancedTransaction) as caught:
        validate(draft)
    assert caught.value.context["imbalance"] == 100


@pytest.mark.invariant
@given(
    debits=st.lists(st.integers(min_value=1, max_value=10_000), min_size=1, max_size=6),
    credit_amounts=st.lists(st.integers(min_value=1, max_value=10_000), min_size=1, max_size=6),
)
@settings(max_examples=300, deadline=None)
def test_validate_accepts_exactly_the_balanced_arrangements(
    debits: list[int], credit_amounts: list[int]
) -> None:
    """``validate`` passes if and only if the two sides sum equal. No exceptions."""
    cash = CHART_REF.ref(AccountCode.MERCHANT_CASH)
    revenue = CHART_REF.ref(AccountCode.MERCHANT_REVENUE)
    entries = tuple(debit(cash, Money(d)) for d in debits) + tuple(
        credit(revenue, Money(c)) for c in credit_amounts
    )
    draft = TransactionDraft(
        merchant_id=MERCHANT,
        txn_type=LedgerTxnType.MANDATE_DEBIT_SETTLED,
        currency=Currency.INR,
        effective_at=AT,
        narration="generated",
        idempotency_key="k",
        entries=entries,
    )
    if sum(debits) == sum(credit_amounts):
        assert validate(draft) is draft
    else:
        with pytest.raises(UnbalancedTransaction):
            validate(draft)


# ---------------------------------------------------------------------------
# Invariant 3: money is integer minor units, and direction is not sign
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@given(amount=st.integers(max_value=0))
def test_entries_must_be_strictly_positive(amount: int) -> None:
    """Zero and negative entries are refused: the side is carried by direction."""
    cash = CHART_REF.ref(AccountCode.MERCHANT_CASH)
    with pytest.raises(ValidationError):
        EntryDraft(account=cash, direction=EntryDirection.DEBIT, amount=Money(amount))


def test_money_cannot_be_built_from_float() -> None:
    """The type system will not let a float anywhere near a posting."""
    with pytest.raises(TypeError):
        Money.from_major(1499.00)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(149900).scale(0.15)  # type: ignore[arg-type]


@given(minor=st.integers(min_value=0, max_value=10**12), parts=st.integers(1, 40))
def test_allocation_conserves_every_paisa(minor: int, parts: int) -> None:
    """Splitting money never creates or destroys a minor unit."""
    original = Money(minor)
    pieces = original.split(parts)
    assert len(pieces) == parts
    assert sum_money(pieces) == original


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@given(amount=money)
@settings(max_examples=100, deadline=None)
def test_reversal_nets_the_original_to_zero(amount: Money) -> None:
    """A reversal restores every account it touched to its prior position."""
    original = grant_concession(ctx(), amount)
    mirrored = reverse_draft(
        original, "ltx_original", effective_at=AT, idempotency_key="rev", reason="operator error"
    )
    validate(mirrored)

    net: dict[str, int] = {}
    for draft in (original, mirrored):
        for entry in draft.entries:
            net[entry.account.id] = net.get(entry.account.id, 0) + entry.signed_minor
    assert all(v == 0 for v in net.values()), net
    assert mirrored.reverses_transaction_id == "ltx_original"
    assert mirrored.txn_type is LedgerTxnType.REVERSAL


def test_reversal_never_edits_the_original() -> None:
    """The mirror is a new draft; the original object is untouched."""
    original = settle_recovered_debit(ctx(), Money(1_499_00))
    before = original.entries
    reverse_draft(original, "ltx_x", effective_at=AT, idempotency_key="r", reason="test")
    assert original.entries is before
    assert original.txn_type is LedgerTxnType.MANDATE_DEBIT_SETTLED


# ---------------------------------------------------------------------------
# Idempotency (invariant 5, at the construction layer)
# ---------------------------------------------------------------------------


@pytest.mark.invariant
@given(amount=money)
@settings(max_examples=50, deadline=None)
def test_idempotency_key_depends_only_on_intent(amount: Money) -> None:
    """Building the same logical posting twice yields the same key.

    This is what makes a retry collapse instead of double-posting. If the key
    varied per call -- by including a timestamp or a fresh id -- a network retry
    would silently pay twice.
    """
    assert settle_recovered_debit(ctx(), amount).idempotency_key == (
        settle_recovered_debit(ctx(), amount).idempotency_key
    )


def test_different_intents_get_different_keys() -> None:
    """A concession and a settlement of the same size are not the same action."""
    amount = Money(500_00)
    assert grant_concession(ctx(), amount).idempotency_key != (
        settle_recovered_debit(ctx(), amount).idempotency_key
    )


def test_different_cases_get_different_keys() -> None:
    a = settle_recovered_debit(ctx(case_id="cse_A"), Money(100_00))
    b = settle_recovered_debit(ctx(case_id="cse_B"), Money(100_00))
    assert a.idempotency_key != b.idempotency_key


# ---------------------------------------------------------------------------
# Economic correctness of the postings
# ---------------------------------------------------------------------------


def test_settlement_moves_receivable_into_cash() -> None:
    draft = settle_recovered_debit(ctx(), Money(1_499_00))
    by_account = {e.account.code: e.direction for e in draft.entries}
    assert by_account[AccountCode.MERCHANT_CASH] is EntryDirection.DEBIT
    assert by_account[AccountCode.CUSTOMER_RECEIVABLE] is EntryDirection.CREDIT


def test_concession_costs_revenue_not_cash_and_consumes_the_budget() -> None:
    """The four legs each carry meaning, and collapsing them would hide one.

    A concession does not cost the merchant cash -- nobody is paid. It costs
    revenue, and it consumes the earmark that authorised it.
    """
    draft = grant_concession(ctx(), Money(200_00))
    sides = {(e.account.code, e.direction) for e in draft.entries}
    assert (AccountCode.CONCESSIONS_GRANTED, EntryDirection.DEBIT) in sides
    assert (AccountCode.CUSTOMER_RECEIVABLE, EntryDirection.CREDIT) in sides
    assert (AccountCode.MERCHANT_CASH, EntryDirection.DEBIT) in sides
    assert (AccountCode.CONCESSION_BUDGET, EntryDirection.CREDIT) in sides
    assert len(draft.entries) == 4


def test_write_off_reduces_the_receivable_recognised_at_case_open() -> None:
    """Recognition then write-off leaves the receivable exactly where it started."""
    amount = Money(999_00)
    opened = recognise_receivable(ctx(), amount)
    closed = write_off(ctx(), amount, "mandate revoked")
    net = 0
    for draft in (opened, closed):
        for entry in draft.entries:
            if entry.account.code is AccountCode.CUSTOMER_RECEIVABLE:
                net += entry.signed_minor
    assert net == 0


def test_a_transaction_may_not_span_merchants() -> None:
    other = ChartOfAccounts.derive("mch_OTHER")
    draft = TransactionDraft(
        merchant_id=MERCHANT,
        txn_type=LedgerTxnType.CHANNEL_COST,
        currency=Currency.INR,
        effective_at=AT,
        narration="cross-tenant",
        idempotency_key="k",
        entries=(
            debit(CHART_REF.ref(AccountCode.CHANNEL_EXPENSE), Money(100)),
            credit(other.ref(AccountCode.MERCHANT_CASH), Money(100)),
        ),
    )
    with pytest.raises(ValidationError, match="span merchants"):
        validate(draft)


def test_single_entry_transactions_are_refused() -> None:
    draft = TransactionDraft(
        merchant_id=MERCHANT,
        txn_type=LedgerTxnType.CHANNEL_COST,
        currency=Currency.INR,
        effective_at=AT,
        narration="lonely",
        idempotency_key="k",
        entries=(debit(CHART_REF.ref(AccountCode.MERCHANT_CASH), Money(100)),),
    )
    with pytest.raises(ValidationError):
        validate(draft)


def test_every_chart_account_declares_a_coherent_normal_direction() -> None:
    """Contra-revenue behaves like a debit even though it lives with revenue."""
    for spec in CHART:
        direction = normal_direction(spec.kind)
        assert direction in (EntryDirection.DEBIT, EntryDirection.CREDIT)
    assert (
        normal_direction(CHART_REF.ref(AccountCode.CONCESSIONS_GRANTED).kind)
        is EntryDirection.DEBIT
    )
    assert (
        normal_direction(CHART_REF.ref(AccountCode.MERCHANT_REVENUE).kind) is EntryDirection.CREDIT
    )


def test_receivable_resolves_to_exactly_one_account() -> None:
    """Never both the control account and the sub-account for one rupee."""
    with_customer = CHART_REF.receivable_for(CUSTOMER)
    without = CHART_REF.receivable_for(None)
    assert with_customer.code is AccountCode.CUSTOMER_RECEIVABLE
    assert without.code is AccountCode.MERCHANT_RECEIVABLE
    assert with_customer.id != without.id


def test_chart_is_deterministic_across_processes() -> None:
    """Two derivations agree, which is what makes the seeded demo reproducible."""
    a = ChartOfAccounts.derive(MERCHANT, customer_ids=(CUSTOMER,))
    b = ChartOfAccounts.derive(MERCHANT, customer_ids=(CUSTOMER,))
    assert [r.id for r in a.all_refs()] == [r.id for r in b.all_refs()]


# ---------------------------------------------------------------------------
# Invariant 8: the concession budget cannot be overspent
# ---------------------------------------------------------------------------


def position(funded: int, settled: int = 0, held: int = 0) -> BudgetPosition:
    return BudgetPosition(funded=Money(funded), settled=Money(settled), held=Money(held))


def request(amount: int, mrr: int = 1_499_00, customer: str = CUSTOMER) -> ReservationRequest:
    return ReservationRequest(
        budget_id="bgt_1",
        merchant_id=MERCHANT,
        case_id=CASE,
        customer_id=customer,
        amount=Money(amount),
        idempotency_key="idm_1",
        subscription_mrr=Money(mrr),
    )


CAPS = {
    "per_action_cap": Money(500_00),
    "per_customer_cap": Money(1_000_00),
    "max_percent_of_mrr": 25,
}


@pytest.mark.invariant
def test_headroom_subtracts_both_settled_and_held() -> None:
    """A hold that might be released is not headroom you can promise elsewhere."""
    p = position(funded=10_000_00, settled=3_000_00, held=2_000_00)
    assert p.headroom == Money(5_000_00)


@pytest.mark.invariant
def test_two_concurrent_concessions_cannot_jointly_overspend() -> None:
    """The second request sees the first one's hold and is refused.

    This is the arithmetic the row lock protects. The lock guarantees the two
    requests observe these positions in sequence rather than both observing the
    empty one; this test proves that given the sequence, the outcome is correct.
    """
    empty = position(funded=300_00)
    first = check_caps(
        request=request(200_00), position=empty, customer_already_conceded=Money(0), **CAPS
    )
    assert first.allowed

    after_first_hold = position(funded=300_00, held=200_00)
    second = check_caps(
        request=request(200_00),
        position=after_first_hold,
        customer_already_conceded=Money(200_00),
        **CAPS,
    )
    assert not second.allowed


@given(
    funded=st.integers(0, 5_000_00),
    settled=st.integers(0, 2_000_00),
    held=st.integers(0, 2_000_00),
    amount=st.integers(1, 500_00),
)
@settings(max_examples=300, deadline=None)
def test_a_reservation_is_never_allowed_beyond_headroom(
    funded: int, settled: int, held: int, amount: int
) -> None:
    """Whatever the caps say, headroom is an absolute ceiling."""
    assume(settled + held <= funded)
    p = position(funded, settled, held)
    verdict = check_caps(
        request=request(amount, mrr=100_000_00),
        position=p,
        per_action_cap=Money(10_000_00),
        per_customer_cap=Money(10_000_00),
        customer_already_conceded=Money(0),
        max_percent_of_mrr=100,
    )
    if verdict.allowed:
        assert Money(amount) <= p.headroom


def test_each_cap_reports_itself_as_the_limiting_one() -> None:
    """An operator must be able to tell which ceiling stopped the agent."""
    big_budget = position(funded=1_000_000_00)

    over_action = check_caps(
        request=request(600_00), position=big_budget, customer_already_conceded=Money(0), **CAPS
    )
    assert over_action.limiting_cap == "per_action_cap"

    over_customer = check_caps(
        request=request(400_00),
        position=big_budget,
        customer_already_conceded=Money(900_00),
        **CAPS,
    )
    assert over_customer.limiting_cap == "per_customer_cap"

    over_mrr = check_caps(
        request=request(400_00, mrr=1_000_00),
        position=big_budget,
        customer_already_conceded=Money(0),
        **CAPS,
    )
    assert over_mrr.limiting_cap == "max_percent_of_mrr"

    no_room = check_caps(
        request=request(100_00),
        position=position(funded=50_00),
        customer_already_conceded=Money(0),
        **CAPS,
    )
    assert no_room.limiting_cap == "headroom"


def test_mrr_ceiling_is_exact_at_the_boundary() -> None:
    """25% of ₹1,499.00 is ₹374.75, and ₹374.75 is allowed while ₹374.76 is not."""
    mrr = Money.from_major(Decimal("1499.00"))
    ceiling = mrr.percent(25)
    assert ceiling == Money(374_75)
    big = position(funded=1_000_000_00)
    assert check_caps(
        request=request(ceiling.minor, mrr=mrr.minor),
        position=big,
        customer_already_conceded=Money(0),
        **CAPS,
    ).allowed
    assert not check_caps(
        request=request(ceiling.minor + 1, mrr=mrr.minor),
        position=big,
        customer_already_conceded=Money(0),
        **CAPS,
    ).allowed


def test_utilisation_reports_in_basis_points() -> None:
    assert position(funded=1_000_00, settled=250_00).utilisation_bps == 2500
    assert position(funded=0).utilisation_bps == 0
    assert position(funded=0, held=1).utilisation_bps == 10_000
