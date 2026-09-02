"""Money as an exact integer quantity of minor units.

Invariant 3 from ``docs/ARCHITECTURE.md``: floats are banned from the money path.
Every amount in Anvil is an integer count of the currency's minor unit (paise for
INR) paired with its currency. Arithmetic across currencies raises rather than
silently coercing, and division is only available in forms that provably conserve
the total.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Self


class Currency(StrEnum):
    """Currencies Anvil can hold. Minor-unit exponent is fixed per currency."""

    INR = "INR"
    USD = "USD"

    @property
    def exponent(self) -> int:
        """Number of decimal places in the minor unit."""
        return _EXPONENTS[self]

    @property
    def symbol(self) -> str:
        return _SYMBOLS[self]


_EXPONENTS: dict[Currency, int] = {Currency.INR: 2, Currency.USD: 2}
_SYMBOLS: dict[Currency, str] = {Currency.INR: "₹", Currency.USD: "$"}


class CurrencyMismatchError(Exception):
    """Raised when an operation mixes two currencies."""

    def __init__(self, left: Currency, right: Currency) -> None:
        super().__init__(f"cannot combine {left} with {right}")
        self.left = left
        self.right = right


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """An exact monetary amount.

    ``minor`` is signed: negative values are legitimate and represent debits in
    contexts that model direction by sign. The ledger itself does not rely on
    sign for direction -- it uses an explicit :class:`~anvil.domain.enums.EntryDirection`.
    """

    minor: int
    currency: Currency = Currency.INR

    def __post_init__(self) -> None:
        if not isinstance(self.minor, int) or isinstance(self.minor, bool):
            raise TypeError(f"Money.minor must be int, got {type(self.minor).__name__}")
        if not isinstance(self.currency, Currency):
            raise TypeError("Money.currency must be a Currency")

    # ---------------------------------------------------------------- builders

    @classmethod
    def zero(cls, currency: Currency = Currency.INR) -> Self:
        return cls(0, currency)

    @classmethod
    def from_major(cls, major: str | int | Decimal, currency: Currency = Currency.INR) -> Self:
        """Build from a major-unit value. Accepts ``str``/``int``/``Decimal`` only.

        ``float`` is rejected deliberately: ``0.1 + 0.2`` is why this class exists.
        """
        if isinstance(major, float):
            raise TypeError("refusing to build Money from float; pass str, int or Decimal")
        scaled = Decimal(major) * (10**currency.exponent)
        quantised = scaled.quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
        return cls(int(quantised), currency)

    @classmethod
    def parse(cls, text: str, currency: Currency = Currency.INR) -> Self:
        """Parse a human string such as ``"1,499.00"`` or ``"₹1499"``."""
        cleaned = text.strip().lstrip("".join(_SYMBOLS.values())).replace(",", "").replace(" ", "")
        if not cleaned:
            raise ValueError("cannot parse empty money string")
        return cls.from_major(Decimal(cleaned), currency)

    # ------------------------------------------------------------- conversions

    @property
    def major(self) -> Decimal:
        """Exact major-unit value. Safe for display and for further Decimal maths."""
        return Decimal(self.minor).scaleb(-self.currency.exponent)

    def format(self, *, with_symbol: bool = True, grouping: bool = True) -> str:
        """Indian digit grouping (``12,34,567.89``) for INR, Western for others."""
        sign = "-" if self.minor < 0 else ""
        units, sub = divmod(abs(self.minor), 10**self.currency.exponent)
        digits = str(units)
        if grouping:
            digits = (
                _group_indian(digits) if self.currency is Currency.INR else _group_western(digits)
            )
        body = f"{digits}.{sub:0{self.currency.exponent}d}" if self.currency.exponent else digits
        prefix = self.currency.symbol if with_symbol else ""
        return f"{sign}{prefix}{body}"

    def __str__(self) -> str:
        return self.format()

    def __repr__(self) -> str:
        return f"Money({self.minor}, {self.currency.value})"

    # -------------------------------------------------------------- arithmetic

    def _check(self, other: Money) -> None:
        if self.currency is not other.currency:
            raise CurrencyMismatchError(self.currency, other.currency)

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor + other.minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.minor - other.minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.minor, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.minor), self.currency)

    def __mul__(self, factor: int) -> Money:
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise TypeError("Money can only be multiplied by int; use scale() for ratios")
        return Money(self.minor * factor, self.currency)

    __rmul__ = __mul__

    def scale(self, ratio: Decimal | str | int, *, rounding: str = ROUND_HALF_EVEN) -> Money:
        """Multiply by an exact ratio, e.g. a 15% concession.

        Uses banker's rounding by default so repeated scaling does not drift upward.
        """
        if isinstance(ratio, float):
            raise TypeError("refusing to scale Money by float; pass Decimal, str or int")
        result = (Decimal(self.minor) * Decimal(ratio)).quantize(Decimal(1), rounding=rounding)
        return Money(int(result), self.currency)

    def percent(self, pct: Decimal | str | int) -> Money:
        """``Money.parse("1000").percent(15)`` -> ₹150.00."""
        if isinstance(pct, float):
            raise TypeError("refusing to take a float percentage of Money")
        return self.scale(Decimal(pct) / Decimal(100))

    def allocate(self, weights: list[int]) -> list[Money]:
        """Split into parts by integer weight, conserving every last paisa.

        The largest-remainder method: no rounding step can create or destroy value,
        so ``sum(m.allocate(w)) == m`` holds for every input.
        """
        if not weights:
            raise ValueError("allocate needs at least one weight")
        if any(w < 0 for w in weights):
            raise ValueError("allocate weights must be non-negative")
        total_weight = sum(weights)
        if total_weight == 0:
            raise ValueError("allocate weights must not sum to zero")

        shares = [self.minor * w // total_weight for w in weights]
        remainder = self.minor - sum(shares)
        # Distribute the remainder one minor unit at a time, largest fractional part first.
        order = sorted(
            range(len(weights)),
            key=lambda i: (-(self.minor * weights[i] % total_weight), i),
        )
        step = 1 if remainder >= 0 else -1
        for i in range(abs(remainder)):
            shares[order[i % len(order)]] += step
        return [Money(s, self.currency) for s in shares]

    def split(self, parts: int) -> list[Money]:
        """Split evenly into ``parts``, conserving the total."""
        if parts < 1:
            raise ValueError("parts must be >= 1")
        return self.allocate([1] * parts)

    # -------------------------------------------------------------- comparison

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.minor < other.minor

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.minor <= other.minor

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.minor > other.minor

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.minor >= other.minor

    # ------------------------------------------------------------- predicates

    @property
    def is_zero(self) -> bool:
        return self.minor == 0

    @property
    def is_positive(self) -> bool:
        return self.minor > 0

    @property
    def is_negative(self) -> bool:
        return self.minor < 0

    def min(self, other: Money) -> Money:
        self._check(other)
        return self if self.minor <= other.minor else other

    def max(self, other: Money) -> Money:
        self._check(other)
        return self if self.minor >= other.minor else other

    def clamp(self, low: Money, high: Money) -> Money:
        self._check(low)
        self._check(high)
        if low > high:
            raise ValueError("clamp bounds inverted")
        return self.max(low).min(high)


def sum_money(items: list[Money], currency: Currency = Currency.INR) -> Money:
    """Sum a possibly-empty list, requiring a currency for the empty case."""
    total = Money.zero(currency)
    for item in items:
        total = total + item
    return total


def _group_indian(digits: str) -> str:
    """``1234567`` -> ``12,34,567``."""
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail])


def _group_western(digits: str) -> str:
    return f"{int(digits):,}"


INR = Currency.INR
ZERO_INR = Money.zero(Currency.INR)
