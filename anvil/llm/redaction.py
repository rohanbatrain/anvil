"""The PII boundary between Anvil and Anthropic.

Nothing reaches a model, a log line or the audit log with a payment identifier
still in it. This module is the single place that decides what counts as an
identifier and what it is replaced with.

Two design decisions are worth stating, because both were choices with a real
alternative:

**Pseudonyms are stable, not opaque.** A naive redactor emits ``[REDACTED]``
everywhere, which destroys exactly the reasoning we want the model to do -- it
can no longer tell that the VPA that failed on Tuesday is the VPA that failed
again on Thursday, or that the phone number in the support ticket belongs to the
customer on the case. So every value maps to a deterministic token derived from
a keyed hash of the value: the same VPA is always ``[[VPA_1A2B3C4D]]`` within a
run, and the model can reason about identity without ever seeing an identifier.

**Precision matters more than recall for card numbers.** A detector that redacts
every sixteen-digit run also redacts order references, UMNs and amounts in
paise, and the resulting prompt is unreadable. Card candidates are therefore
Luhn-checked, and a sixteen-digit number that fails the checksum is left alone
-- it was never a card. Everything else that a checksum cannot arbitrate
(phone numbers, account numbers) is anchored on shape and, where the shape is
genuinely ambiguous, on a nearby keyword.

The keying of the pseudonym matters. Offline runs use a salt derived from
``settings.seed`` so a demo reproduces byte for byte; live runs draw a salt from
the OS CSPRNG per process, so the tokens in one day's audit log cannot be used
to brute-force a card number out of another day's. The token map that reverses
a redaction lives in memory for the lifetime of one request and is never
written anywhere -- persisting it would turn every pseudonym back into PII.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, NamedTuple

from anvil.core.config import Settings, get_settings
from anvil.domain.enums import RunMode


class PiiKind(StrEnum):
    """What a detected span is. The token prefix, so it is visible in prompts."""

    PAN = "PAN"                  # card primary account number
    VPA = "VPA"                  # UPI virtual payment address
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    ACCOUNT = "ACCOUNT"          # bank account number
    IFSC = "IFSC"
    AADHAAR = "AADHAAR"
    NAME = "NAME"


#: Lower sorts first, and first wins when two detectors claim the same span.
#: The ordering encodes specificity: a Luhn-valid card is a card even though the
#: same digits also satisfy the (deliberately loose) bank-account shape.
_PRIORITY: Final[dict[PiiKind, int]] = {
    PiiKind.IFSC: 0,
    PiiKind.PAN: 1,
    PiiKind.AADHAAR: 2,
    PiiKind.VPA: 3,
    PiiKind.EMAIL: 3,
    PiiKind.NAME: 4,
    PiiKind.PHONE: 5,
    PiiKind.ACCOUNT: 6,
}

# --------------------------------------------------------------------- shapes

#: Card candidates. Either an unbroken 13-19 digit run, or the conventional
#: grouped rendering. Grouping is restricted to four-digit blocks so that two
#: adjacent unrelated numbers ("20260902 20260903") cannot accidentally form a
#: nineteen-digit candidate that then passes Luhn one time in ten.
_PAN_RE: Final = re.compile(
    r"(?<![\d.])(?:\d{13,19}|\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{1,7})(?!\.?\d)"
)

#: Aadhaar is twelve digits and never starts with 0 or 1.
_AADHAAR_RE: Final = re.compile(r"(?<![\d.])[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\.?\d)")

#: One shape covers both UPI VPAs and email addresses; they are told apart by
#: whether the right-hand side has a dot. ``ravi@okhdfcbank`` is a payment
#: handle, ``ravi@example.com`` is a mailbox, and they must not be conflated:
#: one is a financial identifier and the other is a contact channel.
_HANDLE_RE: Final = re.compile(
    r"(?<![\w.@-])([A-Za-z0-9][A-Za-z0-9._%+-]{1,63})@([A-Za-z][A-Za-z0-9-]{1,62}"
    r"(?:\.[A-Za-z0-9-]{2,63})*)(?![\w@-])"
)

#: Indian mobile numbers: ten digits starting 6-9, optionally with a country or
#: trunk prefix and one internal separator. The prefix is inside the match so
#: that the whole thing is replaced -- leaving a bare "+91 " in front of a
#: token reads like a bug to anyone auditing the prompt.
_PHONE_RE: Final = re.compile(
    r"(?<![\w.])(?:\+91[ -]?|0091[ -]?|91[ -]|0)?[6-9]\d{4}[ -]?\d{5}(?!\.?\d)"
)

#: Bank account numbers are recognised **only** next to a label, and that is a
#: deliberate loss of recall. They carry no checksum and no distinguishing
#: length: any bare rule wide enough to catch a fourteen-digit account also
#: catches every order id, UMN, RRN and reference number in the bundle, and a
#: prompt in which all of those are pseudonyms is a prompt the model cannot
#: reason over. Labelled is how account numbers actually appear in bank
#: narrations and support tickets, so this is where the recall actually is.
_ACCOUNT_LABELLED_RE: Final = re.compile(
    r"(?i)\b(?:a/c|ac|acc|acct|account)\b[.:# ]*"
    r"(?:no\.?|num|number|#|ending(?: in)?)?[.:# ]*(\d{9,18})(?!\d)"
)

#: IFSC: four letters, a mandatory zero, then six alphanumerics.
_IFSC_RE: Final = re.compile(r"(?<![A-Z0-9])[A-Z]{4}0[A-Z0-9]{6}(?![A-Z0-9])")

_SEPARATORS: Final = str.maketrans("", "", " -+")

#: Token wrapper. Double brackets are chosen because they never occur in bank
#: narrations, gateway error strings or human prose, so a token is unambiguous
#: on the way out and trivially findable on the way back.
_TOKEN_RE: Final = re.compile(r"\[\[([A-Z]+)_([0-9A-F]{8})\]\]")


def luhn_valid(digits: str) -> bool:
    """The Luhn checksum, used to tell a card number from a long number.

    This is the whole reason the redactor can be aggressive about card shapes
    without destroying the prompt: roughly nine in ten arbitrary digit runs of
    card length fail this check and are therefore left in place.
    """
    stripped = digits.translate(_SEPARATORS)
    if not stripped.isdigit() or not 12 <= len(stripped) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(stripped)):
        value = ord(char) - 48
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


class PiiFinding(NamedTuple):
    """One detected span, before any replacement decision is made."""

    kind: PiiKind
    start: int
    end: int
    value: str


class RedactionResult(NamedTuple):
    """Redacted text plus the map needed to reverse it.

    Unpacks as ``text, token_map`` so callers can write the two-tuple form the
    rest of the system expects.
    """

    text: str
    token_map: dict[str, str]


@dataclass(frozen=True, slots=True)
class _Candidate:
    kind: PiiKind
    start: int
    end: int
    value: str

    @property
    def priority(self) -> int:
        return _PRIORITY[self.kind]


class Redactor:
    """Detects payment identifiers and replaces them with stable pseudonyms.

    An instance is the unit of pseudonym stability: two values redacted through
    the same redactor get the same token if and only if they are the same value,
    and that holds across separate ``redact`` calls, which is what lets a
    diagnose -> plan -> compose sequence talk about one customer coherently.

    The instance also accumulates the reverse map. That map is the sensitive
    artifact in this module: it is held in memory, handed to
    :func:`rehydrate` at render time, and never serialised.
    """

    __slots__ = ("_reverse", "_salt")

    def __init__(self, salt: str) -> None:
        if not salt:
            raise ValueError("a redactor needs a non-empty salt; see Redactor.for_settings")
        self._salt = salt
        self._reverse: dict[str, str] = {}

    # ------------------------------------------------------------- construction

    @classmethod
    def deterministic(cls, seed: int) -> Redactor:
        """A redactor whose tokens are reproducible across processes.

        Used offline. Byte-identical demo output is worth more than salt secrecy
        on a machine that holds no real card numbers in the first place.
        """
        return cls(salt=f"anvil-offline-{seed}")

    @classmethod
    def random(cls) -> Redactor:
        """A redactor with a per-process salt from the OS CSPRNG.

        Used live. Without this, the pseudonyms in a persisted audit log would
        be a stable oracle: an attacker holding the log could hash the ten
        million plausible card numbers for an issuer BIN and recover the value.
        """
        return cls(salt=secrets.token_hex(16))

    @classmethod
    def for_settings(cls, settings: Settings) -> Redactor:
        """Pick the salting strategy the run mode calls for."""
        if settings.mode is RunMode.LIVE:
            return cls.random()
        return cls.deterministic(settings.seed)

    # ------------------------------------------------------------------ tokens

    def token_for(self, kind: PiiKind, value: str) -> str:
        """The stable pseudonym for one value, registering it for rehydration."""
        canonical = _canonical_value(kind, value)
        digest = hashlib.blake2b(
            f"{kind.value}\x1f{canonical}".encode(),
            key=self._salt.encode(),
            digest_size=4,
        ).hexdigest().upper()
        token = f"[[{kind.value}_{digest}]]"
        self._reverse.setdefault(token, value)
        return token

    @property
    def token_map(self) -> dict[str, str]:
        """A copy of everything this redactor has seen. Never persist this."""
        return dict(self._reverse)

    def forget(self) -> None:
        """Drop the reverse map. Call this when a request is finished with."""
        self._reverse.clear()

    # --------------------------------------------------------------- detection

    def find(self, text: str, *, names: Sequence[str] = ()) -> list[PiiFinding]:
        """Every identifier in ``text``, with overlaps already resolved.

        Exposed separately from :meth:`redact` because the output guardrail
        needs to ask "is there anything left in here?" without rewriting the
        string.
        """
        return [
            PiiFinding(c.kind, c.start, c.end, c.value)
            for c in _resolve(_candidates(text, names))
        ]

    def redact(self, text: str, *, names: Sequence[str] = ()) -> RedactionResult:
        """Replace every identifier with its pseudonym.

        ``names`` are customer names supplied by the caller. Names cannot be
        detected by shape without wrecking ordinary prose, so they are matched
        only when the first-party record says what they are.
        """
        if not text:
            return RedactionResult(text, {})
        local: dict[str, str] = {}
        out: list[str] = []
        cursor = 0
        for candidate in _resolve(_candidates(text, names)):
            token = self.token_for(candidate.kind, candidate.value)
            local.setdefault(token, candidate.value)
            out.append(text[cursor : candidate.start])
            out.append(token)
            cursor = candidate.end
        out.append(text[cursor:])
        return RedactionResult("".join(out), local)

    def redact_value(self, value: Any, *, names: Sequence[str] = ()) -> tuple[Any, dict[str, str]]:
        """Redact every string inside a JSON-shaped structure.

        Prompt payloads are assembled as nested dicts of first-party data, so
        the boundary has to be able to walk one rather than only a flat string.
        Keys are redacted as well as values: a dict keyed by VPA would otherwise
        leak through the key.
        """
        collected: dict[str, str] = {}

        def walk(node: Any) -> Any:
            if isinstance(node, str):
                redacted, found = self.redact(node, names=names)
                collected.update(found)
                return redacted
            if isinstance(node, Mapping):
                return {walk(k): walk(v) for k, v in node.items()}
            if isinstance(node, list | tuple):
                return [walk(item) for item in node]
            return node

        return walk(value), collected


def rehydrate(text: str, token_map: Mapping[str, str]) -> str:
    """Put the real values back, for the narrow case that needs them.

    The only legitimate caller is the channel adapter at send time: an SMS that
    says "your payment from [[VPA_1A2B3C4D]] failed" is useless to a human. The
    rehydrated string is handed straight to the provider and is never logged,
    never checkpointed and never written to the audit record -- the redacted
    form is what persists.

    Tokens that are absent from the map are left as they are rather than
    raising, because a partially-rehydrated message is still deliverable while
    a raised exception at send time loses the message entirely.

    Rehydration is a round trip up to *rendering*, not byte for byte. One
    identifier written three ways -- ``+91 98765 43210``, ``09876543210``,
    ``9876543210`` -- deliberately collapses onto one pseudonym, so it comes
    back in whichever form was seen first. Losing that distinction is the point:
    if the three renderings survived as three tokens the model would read them
    as three customers.
    """
    if not token_map:
        return text

    def substitute(match: re.Match[str]) -> str:
        return token_map.get(match.group(0), match.group(0))

    return _TOKEN_RE.sub(substitute, text)


def unresolved_tokens(text: str, token_map: Mapping[str, str]) -> list[str]:
    """Tokens in ``text`` that ``token_map`` cannot reverse.

    A non-empty result means the message was assembled from more than one
    redaction scope, which is a bug worth surfacing before the message is sent.
    """
    return [m.group(0) for m in _TOKEN_RE.finditer(text) if m.group(0) not in token_map]


# --------------------------------------------------------------------- module

_default: Redactor | None = None


def default_redactor() -> Redactor:
    """The process-wide redactor, built from settings on first use.

    Pseudonym stability is a property of a redactor instance, so the convenience
    functions below need somewhere to keep one. Anything that owns a request
    scope -- the LLM client, the channel dispatcher -- should hold its own via
    :meth:`Redactor.for_settings` and drop it when the request ends, rather than
    letting the process map grow for the life of the worker.
    """
    global _default
    if _default is None:
        _default = Redactor.for_settings(get_settings())
    return _default


def set_default_redactor(redactor: Redactor) -> None:
    """Install the process redactor. Called once during application startup."""
    global _default
    _default = redactor


def redact(text: str, *, names: Sequence[str] = ()) -> RedactionResult:
    """Redact through the process-wide redactor."""
    return default_redactor().redact(text, names=names)


def find_pii(text: str, *, names: Sequence[str] = ()) -> list[PiiFinding]:
    """Detect without rewriting. Used by the output guardrail."""
    return default_redactor().find(text, names=names)


def contains_pii(text: str, *, names: Sequence[str] = ()) -> bool:
    """True when anything in ``text`` still looks like an identifier."""
    return bool(_resolve(_candidates(text, names)))


# ------------------------------------------------------------------ internals


def _canonical_value(kind: PiiKind, value: str) -> str:
    """Fold formatting differences so one identifier gets one token.

    ``4111 1111 1111 1111`` and ``4111-1111-1111-1111`` are the same card, and
    ``Ravi@OKHDFCBANK`` is the same handle as ``ravi@okhdfcbank``. Without this
    the model would see two tokens and conclude there are two customers.
    """
    if kind in (PiiKind.PAN, PiiKind.ACCOUNT, PiiKind.AADHAAR, PiiKind.PHONE):
        digits = value.translate(_SEPARATORS)
        return _strip_dialling_prefix(digits) if kind is PiiKind.PHONE else digits
    return value.strip().lower()


def _strip_dialling_prefix(digits: str) -> str:
    """Fold ``+91 98765 43210``, ``09876543210`` and ``9876543210`` together.

    Length-guarded rather than a blind ``removeprefix``: a subscriber number can
    itself begin ``91``, and stripping that would merge two different customers
    onto one pseudonym.
    """
    if len(digits) == 14 and digits.startswith("0091"):
        return digits[4:]
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return digits


def _candidates(text: str, names: Sequence[str]) -> Iterator[_Candidate]:
    for match in _IFSC_RE.finditer(text):
        yield _Candidate(PiiKind.IFSC, match.start(), match.end(), match.group(0))

    for match in _PAN_RE.finditer(text):
        if luhn_valid(match.group(0)):
            yield _Candidate(PiiKind.PAN, match.start(), match.end(), match.group(0))

    for match in _AADHAAR_RE.finditer(text):
        yield _Candidate(PiiKind.AADHAAR, match.start(), match.end(), match.group(0))

    for match in _HANDLE_RE.finditer(text):
        kind = PiiKind.EMAIL if "." in match.group(2) else PiiKind.VPA
        yield _Candidate(kind, match.start(), match.end(), match.group(0))

    for match in _PHONE_RE.finditer(text):
        yield _Candidate(PiiKind.PHONE, match.start(), match.end(), match.group(0))

    for match in _ACCOUNT_LABELLED_RE.finditer(text):
        yield _Candidate(PiiKind.ACCOUNT, match.start(1), match.end(1), match.group(1))

    for pattern in _name_patterns(names):
        for match in pattern.finditer(text):
            yield _Candidate(PiiKind.NAME, match.start(), match.end(), match.group(0))


def _name_patterns(names: Sequence[str]) -> Iterable[re.Pattern[str]]:
    """One pattern per supplied name, longest first.

    Longest-first matters for "Ravi" and "Ravi Kumar": the shorter name would
    otherwise claim the span and leave the surname in the clear.
    """
    cleaned = sorted({n.strip() for n in names if n and n.strip()}, key=len, reverse=True)
    for name in cleaned:
        yield re.compile(rf"(?<![\w]){re.escape(name)}(?![\w])", re.IGNORECASE)


def _resolve(candidates: Iterable[_Candidate]) -> list[_Candidate]:
    """Drop overlaps, keeping the earliest start and then the most specific.

    Detectors are deliberately allowed to overlap -- a VPA contains something
    phone-shaped, a card contains something account-shaped -- and this is where
    that is arbitrated once, in one place, rather than by regex tricks spread
    across eight patterns.
    """
    ordered = sorted(candidates, key=lambda c: (c.start, c.priority, -(c.end - c.start)))
    kept: list[_Candidate] = []
    cursor = -1
    for candidate in ordered:
        if candidate.start >= cursor:
            kept.append(candidate)
            cursor = candidate.end
    return kept
