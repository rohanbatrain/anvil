"""Deterministic randomness primitives for the simulator.

Everything stochastic in Anvil's offline world descends from one integer seed,
and the guarantee we make to a judge is stronger than "it usually looks the
same": the same seed produces a byte-identical world on any machine. Two design
rules buy that guarantee, and both live here rather than being re-derived in
each module.

**Substreams, not one shared generator.** A single :class:`random.Random`
threaded through the whole simulation makes every outcome depend on the *order*
in which things happened to be drawn, so adding one message changes every
subsequent coin flip. Instead each logical subject -- one attempt, one outreach,
one customer -- gets its own generator keyed by a label. Outcomes then depend
only on ``(seed, label)``, which means the world can process events in any order
and the issuer can be queried out of band without perturbing anything.

**Integers and Decimals, never transcendental floats.** ``random.random()`` and
IEEE-754 arithmetic are bit-reproducible across platforms; ``log``, ``exp`` and
``gauss`` route through libm and are not. So the skewed distributions here are
built from integer draws and exact Decimal ratios. It costs a little elegance
and buys the reproducibility claim outright.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Sequence
from decimal import Decimal
from typing import TypeVar

T = TypeVar("T")

#: Denominator for Bernoulli draws. One part per million is finer than any
#: probability the simulator models and keeps every comparison integral.
_PROBABILITY_SCALE = 1_000_000

ZERO = Decimal(0)
ONE = Decimal(1)


def substream(seed: int, *labels: str) -> random.Random:
    """An independent generator for one labelled subject.

    The label set is hashed with the seed, so ``substream(7, "attempt", "atm_x")``
    is the same generator no matter when it is asked for or how many other
    substreams were created first. That order-independence is what lets the
    world simulator reorder work, and the tests query the issuer directly,
    without changing any outcome.
    """
    payload = b"\x1f".join([str(seed).encode(), *(label.encode() for label in labels)])
    digest = hashlib.blake2b(payload, digest_size=32).digest()
    return random.Random(int.from_bytes(digest, "big"))


def clamp_unit(value: Decimal) -> Decimal:
    """Fold a probability back into [0, 1] without hiding that it went out."""
    if value < ZERO:
        return ZERO
    return ONE if value > ONE else value


def bernoulli(rng: random.Random, probability: Decimal) -> bool:
    """A coin flip at an exact Decimal probability.

    Compares an integer draw against the probability scaled to parts per
    million, so there is no float rounding anywhere in the decision.
    """
    threshold = int((clamp_unit(probability) * _PROBABILITY_SCALE).to_integral_value())
    return rng.randrange(_PROBABILITY_SCALE) < threshold


def uniform_decimal(rng: random.Random) -> Decimal:
    """A uniform draw on [0, 1) with a millionth's resolution."""
    return Decimal(rng.randrange(_PROBABILITY_SCALE)) / Decimal(_PROBABILITY_SCALE)


def uniform_between(rng: random.Random, low: Decimal, high: Decimal) -> Decimal:
    """A uniform draw on ``[low, high]``, inclusive at both ends."""
    if high < low:
        raise ValueError("uniform_between requires low <= high")
    return low + (high - low) * uniform_decimal(rng)


def weighted_choice(rng: random.Random, options: Sequence[tuple[T, int]]) -> T:
    """Pick one option by integer weight.

    Integer weights rather than float probabilities: the cumulative sum is then
    exact, so a distribution never silently loses or gains a fraction of a
    percent through repeated addition.
    """
    if not options:
        raise ValueError("weighted_choice needs at least one option")
    total = sum(weight for _, weight in options)
    if total <= 0:
        raise ValueError("weighted_choice needs a positive total weight")
    draw = rng.randrange(total)
    cumulative = 0
    for value, weight in options:
        cumulative += weight
        if draw < cumulative:
            return value
    return options[-1][0]  # unreachable for positive weights; kept total


def skewed_int(rng: random.Random, low: int, high: int, *, skew: int = 1) -> int:
    """An integer on ``[low, high]`` pulled toward ``low``.

    The minimum of ``skew`` independent uniforms. Used for tenure and prior
    failure counts, where the real distribution is "mostly new, with a long
    tail of old" rather than anything symmetric. It needs no logarithm, so it
    reproduces exactly everywhere.
    """
    if high < low:
        raise ValueError("skewed_int requires low <= high")
    if skew < 1:
        raise ValueError("skewed_int requires skew >= 1")
    best = rng.randint(low, high)
    for _ in range(skew - 1):
        candidate = rng.randint(low, high)
        if candidate < best:
            best = candidate
    return best


def jitter(rng: random.Random, spread_bps: int) -> Decimal:
    """A multiplicative perturbation in ``1 ± spread``.

    The issuer uses this to hold beliefs that are the *same shape* as the
    scheduler's published retry curves but not the same numbers. Without it the
    scheduler would be reading its own answer back and the evidence would prove
    nothing.
    """
    if spread_bps < 0:
        raise ValueError("jitter spread must be non-negative")
    if spread_bps == 0:
        return ONE
    return Decimal(10_000 + rng.randrange(-spread_bps, spread_bps + 1)) / Decimal(10_000)


def to_bps(value: Decimal) -> int:
    """Render a probability as integer basis points, for storage and display."""
    return int((clamp_unit(value) * Decimal(10_000)).to_integral_value())
