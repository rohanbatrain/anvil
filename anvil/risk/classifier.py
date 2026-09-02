"""Deterministic failure classification, and the precise point at which it gives up.

:mod:`anvil.domain.taxonomy` answers one narrow question: does this exact token
appear in one of our code tables? Real failures do not arrive as one clean
token. They arrive as a bundle -- a gateway error slug, a free-text gateway
description, a bank narration typed by a settlement system from 1998, and
sometimes a hint about which rail the debit rode. This module reads the whole
bundle.

It exists for three reasons the bare lookup cannot serve:

1. **Ambiguity is real.** ``"05"`` is a revoked mandate on NACH and a plain
   "do not honour" on cards. A lookup that searches namespaces in a fixed order
   silently picks one. Here, an ambiguous code with no rail hint is reported as
   ambiguous and escalated, because guessing between "the customer cancelled"
   and "the issuer said no this time" is exactly the mistake that sends an
   insulting dunning email.
2. **Corroboration is evidence.** One weak signal is not enough to act on; two
   independent weak signals that agree usually are. That rule is written down
   here as a threshold and a margin rather than living in someone's judgement.
3. **Escalation must be a value, not a side effect.** Nothing in this module
   calls a model. When the deterministic path cannot resolve the bundle it
   returns an :class:`UnresolvedClassification` carrying everything the LLM
   classifier will need. The caller decides whether to spend a model call.

The output vocabulary is closed: every result is a member of
:class:`~anvil.domain.enums.FailureClass`, and this module never widens it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Final

from anvil.domain.enums import AuthorisationType, FailureClass
from anvil.domain.taxonomy import CODE_NAMESPACES, classify_code

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
# Every weight below is in basis points of confidence. They are constants rather
# than tuned parameters: the deterministic path must give the same answer today
# and in six months, and a number that drifts is a number nobody can audit.

#: Below this, the bundle goes to the model. Chosen so that a single free-text
#: phrase match never resolves on its own but a recognised code always does.
RESOLVE_THRESHOLD_BPS: Final = 6000

#: The winner must beat the runner-up by at least this much. Two classes within
#: a hair of each other is precisely the situation a language model handles
#: better than a table, so it is escalated rather than decided by tie-break.
DECISION_MARGIN_BPS: Final = 1500

#: Added for each *additional* field that independently supports the same class.
#: Agreement between a gateway description and a bank narration is genuine
#: evidence -- they come from different systems and are not copies of each other.
CORROBORATION_BONUS_BPS: Final = 1000

#: Field priority. A structured code beats prose; the gateway's own description
#: beats a bank narration, which is the noisiest input in the bundle.
FIELD_RAW_CODE: Final = "raw_code"
FIELD_GATEWAY_DESCRIPTION: Final = "gateway_description"
FIELD_BANK_NARRATION: Final = "bank_narration"

_FIELD_ORDER: Final = (FIELD_RAW_CODE, FIELD_GATEWAY_DESCRIPTION, FIELD_BANK_NARRATION)

#: Confidence contributed by one piece of evidence, by field and by kind.
#:
#: ``hinted``    -- exact code hit inside the namespace the caller named.
#: ``unique``    -- exact code hit that only one namespace claims.
#: ``ambiguous`` -- exact code hit claimed by several namespaces, no rail hint.
#:                  Deliberately below the resolve threshold on its own.
#: ``text``      -- the whole field matched a known textual code slug.
#: ``phrase``    -- a natural-language phrase matched.
_EVIDENCE_WEIGHTS: Final[dict[str, dict[str, int]]] = {
    FIELD_RAW_CODE: {
        "hinted": 10000,
        "unique": 9000,
        "ambiguous": 4000,
        "text": 8500,
        "phrase": 6500,
    },
    FIELD_GATEWAY_DESCRIPTION: {
        "hinted": 8000,
        "unique": 7500,
        "ambiguous": 3500,
        "text": 7000,
        "phrase": 5500,
    },
    FIELD_BANK_NARRATION: {
        "hinted": 7000,
        "unique": 6500,
        "ambiguous": 3000,
        "text": 6000,
        "phrase": 5000,
    },
}

#: Namespaces that hold rail reason codes. ``text`` is handled separately because
#: its keys are whole slugs rather than tokens.
_RAIL_NAMESPACES: Final = ("upi", "nach", "card")

#: What callers actually pass as a rail hint, folded onto a namespace name. The
#: authorisation type is accepted directly so the graph can hand over the
#: mandate it is working without translating first.
_RAIL_ALIASES: Final[dict[str, str]] = {
    "upi": "upi",
    "upi_autopay": "upi",
    "autopay": "upi",
    "upi_mandate": "upi",
    "npci": "upi",
    "nach": "nach",
    "enach": "nach",
    "e_nach": "nach",
    "emandate": "nach",
    "e_mandate": "nach",
    "ach": "nach",
    "card": "card",
    "cards": "card",
    "card_mandate": "card",
    "si": "card",
}

#: Marker words that license reading the *next* token as a reason code. Without
#: one of these, a bare number in a narration ("credited 26 aug") is just a
#: number, and reading it as NACH return code 26 would be a fabrication.
_CODE_MARKERS: Final[frozenset[str]] = frozenset(
    {"npci", "rc", "code", "reason", "err", "error", "return", "resp", "response", "reject"}
)

#: Phrase rules, matched on whitespace-normalised text with word boundaries.
#: Multi-word and specific by design: ``mandate expired`` must not be caught by
#: a generic rule about expiry, because an expired mandate and an expired card
#: lead to completely different recovery journeys.
_PHRASE_RULES: Final[tuple[tuple[str, FailureClass], ...]] = (
    # --- balance ---------------------------------------------------------
    ("insufficient", FailureClass.INSUFFICIENT_FUNDS),
    ("insufficient funds", FailureClass.INSUFFICIENT_FUNDS),
    ("insufficient balance", FailureClass.INSUFFICIENT_FUNDS),
    ("bal low", FailureClass.INSUFFICIENT_FUNDS),
    ("low bal", FailureClass.INSUFFICIENT_FUNDS),
    ("low balance", FailureClass.INSUFFICIENT_FUNDS),
    ("balance low", FailureClass.INSUFFICIENT_FUNDS),
    ("no funds", FailureClass.INSUFFICIENT_FUNDS),
    ("not enough balance", FailureClass.INSUFFICIENT_FUNDS),
    ("funds insufficient", FailureClass.INSUFFICIENT_FUNDS),
    # --- instrument ------------------------------------------------------
    ("card expired", FailureClass.INSTRUMENT_EXPIRED),
    ("expired card", FailureClass.INSTRUMENT_EXPIRED),
    ("card has expired", FailureClass.INSTRUMENT_EXPIRED),
    ("instrument expired", FailureClass.INSTRUMENT_EXPIRED),
    ("expiry date", FailureClass.INSTRUMENT_EXPIRED),
    # --- rail / issuer ----------------------------------------------------
    ("bank down", FailureClass.ISSUER_TECHNICAL),
    ("issuer down", FailureClass.ISSUER_TECHNICAL),
    ("bank unavailable", FailureClass.ISSUER_TECHNICAL),
    ("issuer unavailable", FailureClass.ISSUER_TECHNICAL),
    ("remitter bank", FailureClass.ISSUER_TECHNICAL),
    ("temporarily unavailable", FailureClass.ISSUER_TECHNICAL),
    ("system malfunction", FailureClass.ISSUER_TECHNICAL),
    ("technical reasons", FailureClass.ISSUER_TECHNICAL),
    ("technical decline", FailureClass.ISSUER_TECHNICAL),
    ("timed out", FailureClass.ISSUER_TECHNICAL),
    ("timeout", FailureClass.ISSUER_TECHNICAL),
    ("try again later", FailureClass.ISSUER_TECHNICAL),
    ("server error", FailureClass.ISSUER_TECHNICAL),
    ("gateway error", FailureClass.ISSUER_TECHNICAL),
    # --- limits -----------------------------------------------------------
    ("limit exceeded", FailureClass.LIMIT_EXCEEDED),
    ("exceeds limit", FailureClass.LIMIT_EXCEEDED),
    ("exceeds the limit", FailureClass.LIMIT_EXCEEDED),
    ("amount exceeds", FailureClass.LIMIT_EXCEEDED),
    ("per transaction limit", FailureClass.LIMIT_EXCEEDED),
    ("daily limit", FailureClass.LIMIT_EXCEEDED),
    # --- mandate ----------------------------------------------------------
    ("mandate cancelled", FailureClass.MANDATE_REVOKED),
    ("mandate canceled", FailureClass.MANDATE_REVOKED),
    ("mandate revoked", FailureClass.MANDATE_REVOKED),
    ("mandate not found", FailureClass.MANDATE_REVOKED),
    ("mandate expired", FailureClass.MANDATE_REVOKED),
    ("umn not found", FailureClass.MANDATE_REVOKED),
    ("not registered", FailureClass.MANDATE_REVOKED),
    ("deregistered", FailureClass.MANDATE_REVOKED),
    ("no mandate", FailureClass.MANDATE_REVOKED),
    ("mandate on hold", FailureClass.MANDATE_PAUSED),
    ("mandate paused", FailureClass.MANDATE_PAUSED),
    ("payment stopped", FailureClass.MANDATE_PAUSED),
    ("stop payment", FailureClass.MANDATE_PAUSED),
    ("stopped by drawer", FailureClass.MANDATE_PAUSED),
    # --- account ----------------------------------------------------------
    ("account closed", FailureClass.ACCOUNT_CLOSED),
    ("account blocked", FailureClass.ACCOUNT_CLOSED),
    ("account frozen", FailureClass.ACCOUNT_CLOSED),
    ("account dormant", FailureClass.ACCOUNT_CLOSED),
    ("dormant account", FailureClass.ACCOUNT_CLOSED),
    ("no such account", FailureClass.ACCOUNT_CLOSED),
    ("invalid account", FailureClass.ACCOUNT_CLOSED),
    ("account does not exist", FailureClass.ACCOUNT_CLOSED),
    # --- risk ---------------------------------------------------------------
    ("do not honour", FailureClass.RISK_DECLINED),
    ("do not honor", FailureClass.RISK_DECLINED),
    ("suspected fraud", FailureClass.RISK_DECLINED),
    ("fraud", FailureClass.RISK_DECLINED),
    ("declined by bank", FailureClass.RISK_DECLINED),
    ("restricted card", FailureClass.RISK_DECLINED),
    ("security violation", FailureClass.RISK_DECLINED),
    ("velocity", FailureClass.RISK_DECLINED),
    ("cvv", FailureClass.RISK_DECLINED),
    # --- authentication ------------------------------------------------------
    ("authentication required", FailureClass.AUTH_REQUIRED),
    ("additional factor", FailureClass.AUTH_REQUIRED),
    ("step up", FailureClass.AUTH_REQUIRED),
    ("otp", FailureClass.AUTH_REQUIRED),
    ("mpin", FailureClass.AUTH_REQUIRED),
    ("re authenticate", FailureClass.AUTH_REQUIRED),
)

_TOKEN_SPLIT: Final = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalise_text(raw: str) -> str:
    """Fold arbitrary gateway or bank prose into a matchable token stream.

    Punctuation carries no meaning in these strings and varies by sender, so it
    becomes whitespace: ``"A/c bal low - NPCI:U30"`` becomes ``"a c bal low npci
    u30"``. Matching then happens against the padded form, which gives word
    boundaries for free without a regex per phrase.
    """
    return " ".join(t for t in _TOKEN_SPLIT.split(raw.lower()) if t)


def normalise_rail(hint: str | AuthorisationType | None) -> str | None:
    """Fold a rail hint onto a code namespace, or ``None`` if it names no rail.

    Reserve Pay blocks and delegated-agent authority are authorisation shapes,
    not rails -- they ride UPI underneath but the caller has told us nothing
    about which code table applies, so we say so rather than assume.
    """
    if hint is None:
        return None
    token = str(hint).strip().lower().replace("-", "_").replace(" ", "_")
    return _RAIL_ALIASES.get(token)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClassificationSignals:
    """Everything known about one failure at classification time.

    All four fields are optional because real ingestion is lossy: a NACH return
    file gives a code and nothing else, a card gateway gives a description and a
    code, a UPI webhook sometimes gives only prose.
    """

    raw_code: str | None = None
    gateway_description: str | None = None
    bank_narration: str | None = None
    rail_hint: str | AuthorisationType | None = None

    def populated_fields(self) -> tuple[str, ...]:
        """Which of the three text fields actually carry content."""
        present = {
            FIELD_RAW_CODE: self.raw_code,
            FIELD_GATEWAY_DESCRIPTION: self.gateway_description,
            FIELD_BANK_NARRATION: self.bank_narration,
        }
        return tuple(name for name in _FIELD_ORDER if (present[name] or "").strip())


@dataclass(frozen=True, slots=True)
class ClassificationEvidence:
    """One reason to believe a bundle means a particular failure class.

    Kept as a first-class record rather than collapsed into a score because the
    audit log has to be able to answer "why did you decide that?" months later,
    and because the LLM classifier is handed the same records when the
    deterministic path escalates.
    """

    field_name: str
    kind: str
    namespace: str
    token: str
    failure_class: FailureClass
    weight_bps: int

    def describe(self) -> str:
        return (
            f"{self.field_name} matched {self.token!r} in the {self.namespace} "
            f"namespace ({self.kind}, {self.weight_bps} bps)"
        )


def _code_evidence(field_name: str, text: str, rail: str | None) -> list[ClassificationEvidence]:
    """Pull rail reason codes out of one field.

    A structured ``raw_code`` field is trusted to contain a code, so every token
    in it is a candidate. Prose is not: there, a bare number only counts if a
    marker word ("RC", "NPCI", "reason") immediately precedes it. That single
    rule is what keeps ``"settled 26 Aug"`` from being read as NACH return code
    26 -- a false classification is worse than no classification, because it
    silently chooses the wrong recovery journey.
    """
    weights = _EVIDENCE_WEIGHTS[field_name]
    structured = field_name == FIELD_RAW_CODE
    tokens = normalise_text(text).split()
    out: list[ClassificationEvidence] = []
    seen: set[str] = set()

    for index, token in enumerate(tokens):
        if token in seen:
            continue
        preceded_by_marker = index > 0 and tokens[index - 1] in _CODE_MARKERS
        if not (structured or preceded_by_marker or _looks_like_rail_code(token)):
            continue
        hits = {
            namespace: CODE_NAMESPACES[namespace][token.upper()]
            for namespace in _RAIL_NAMESPACES
            if token.upper() in CODE_NAMESPACES[namespace]
        }
        if not hits:
            continue
        seen.add(token)
        if rail is not None and rail in hits:
            out.append(
                ClassificationEvidence(
                    field_name, "code", rail, token, hits[rail], weights["hinted"]
                )
            )
            continue
        distinct = set(hits.values())
        if len(distinct) == 1:
            namespace, failure_class = next(iter(hits.items()))
            out.append(
                ClassificationEvidence(
                    field_name, "code", namespace, token, failure_class, weights["unique"]
                )
            )
            continue
        for namespace, failure_class in sorted(hits.items()):
            out.append(
                ClassificationEvidence(
                    field_name, "code", namespace, token, failure_class, weights["ambiguous"]
                )
            )
    return out


def _looks_like_rail_code(token: str) -> bool:
    """A token like ``u30``, ``z9`` or ``b3`` reads as a code even in free prose.

    Requiring both a letter and a digit is the whole test. Pure-alpha codes such
    as ``ZA`` are excluded on purpose: ``"za"`` and ``"am"`` are far too likely
    to be ordinary words or a timestamp fragment, so those only resolve from a
    structured code field or behind a marker word.
    """
    return (
        2 <= len(token) <= 4 and any(c.isdigit() for c in token) and any(c.isalpha() for c in token)
    )


def _text_evidence(field_name: str, text: str) -> ClassificationEvidence | None:
    """Match the whole field against the textual code slugs in the taxonomy."""
    failure_class = classify_code(text, namespace="text")
    if failure_class is None:
        return None
    return ClassificationEvidence(
        field_name,
        "text",
        "text",
        normalise_text(text).replace(" ", "_"),
        failure_class,
        _EVIDENCE_WEIGHTS[field_name]["text"],
    )


def _phrase_evidence(field_name: str, text: str) -> list[ClassificationEvidence]:
    """Match natural-language phrases, longest first.

    Longest-first matters: ``"mandate expired"`` and ``"card expired"`` must win
    over any shorter overlapping rule, and only the most specific match per
    class is kept so that a wordy narration does not out-vote a terse one.
    """
    padded = f" {normalise_text(text)} "
    weight = _EVIDENCE_WEIGHTS[field_name]["phrase"]
    best: dict[FailureClass, str] = {}
    for phrase, failure_class in sorted(_PHRASE_RULES, key=lambda r: -len(r[0])):
        if f" {phrase} " in padded and failure_class not in best:
            best[failure_class] = phrase
    return [
        ClassificationEvidence(field_name, "phrase", "phrase", phrase, failure_class, weight)
        for failure_class, phrase in sorted(best.items(), key=lambda kv: kv[0].value)
    ]


def gather_evidence(
    signals: ClassificationSignals, rail: str | None = None
) -> tuple[ClassificationEvidence, ...]:
    """Every deterministic reason to believe anything about this bundle.

    Exposed rather than kept private because the console renders it directly:
    an operator overriding a classification should be able to see what the
    machine saw.
    """
    resolved_rail = rail if rail is not None else normalise_rail(signals.rail_hint)
    sources = (
        (FIELD_RAW_CODE, signals.raw_code),
        (FIELD_GATEWAY_DESCRIPTION, signals.gateway_description),
        (FIELD_BANK_NARRATION, signals.bank_narration),
    )
    out: list[ClassificationEvidence] = []
    for field_name, value in sources:
        if not value or not value.strip():
            continue
        out.extend(_code_evidence(field_name, value, resolved_rail))
        text_hit = _text_evidence(field_name, value)
        if text_hit is not None:
            out.append(text_hit)
        out.extend(_phrase_evidence(field_name, value))
    return tuple(out)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeterministicClassification:
    """A classification the code tables reached on their own.

    ``matched_code`` and ``matched_namespace`` are recorded because the
    ``classified_deterministically`` column on a recovery case is only worth
    having if it can be checked: given these two fields anyone can re-run the
    lookup by hand and get the same answer.
    """

    resolved: ClassVar[bool] = True
    should_escalate: ClassVar[bool] = False

    failure_class: FailureClass
    confidence_bps: int
    matched_code: str
    matched_namespace: str
    matched_field: str
    evidence: tuple[ClassificationEvidence, ...] = ()
    conflicting: tuple[ClassificationEvidence, ...] = ()

    @property
    def corroborating_fields(self) -> tuple[str, ...]:
        """Distinct fields that independently pointed at the chosen class."""
        return tuple(
            sorted(
                {e.field_name for e in self.evidence if e.failure_class is self.failure_class},
                key=_FIELD_ORDER.index,
            )
        )

    @property
    def has_conflict(self) -> bool:
        """True when some signal pointed somewhere else and was outweighed.

        Not an error: a bank narration saying "insufficient" alongside an issuer
        code saying "mandate revoked" happens, and the code wins. It is recorded
        so a pattern of conflicts becomes visible instead of being averaged away.
        """
        return bool(self.conflicting)

    def describe(self) -> str:
        base = (
            f"{self.failure_class.value} from {self.matched_code!r} "
            f"({self.matched_namespace}, via {self.matched_field})"
        )
        if self.conflicting:
            others = ", ".join(sorted({e.failure_class.value for e in self.conflicting}))
            return f"{base}; outweighed conflicting evidence for {others}"
        return base


#: Why the deterministic path declined to decide. Each maps to a different
#: prompt shape for the LLM classifier, which is why they are distinguished.
NO_RECOGNISED_SIGNAL: Final = "no_recognised_signal"
WEAK_EVIDENCE: Final = "weak_evidence"
CONFLICTING_SIGNALS: Final = "conflicting_signals"


@dataclass(frozen=True, slots=True)
class UnresolvedClassification:
    """The deterministic path looked, and is telling the caller to escalate.

    This is a *result*, not an exception, and not a model call. The module that
    owns the model budget decides whether this bundle is worth a token spend --
    a ₹49 case and a ₹49,000 case deserve different answers to that question,
    and the classifier is not the place to know which this is.
    """

    resolved: ClassVar[bool] = False
    should_escalate: ClassVar[bool] = True

    reason: str
    signals: ClassificationSignals
    evidence: tuple[ClassificationEvidence, ...] = ()
    #: Ranked shortlist of (class, confidence) the model should choose between.
    #: Empty when nothing at all was recognised.
    candidates: tuple[tuple[FailureClass, int], ...] = ()

    @property
    def best_guess(self) -> FailureClass | None:
        """The leading candidate, for display only.

        Never persisted as the classification. Showing an operator "probably
        insufficient funds, but we are not sure" is useful; recording it as
        fact would defeat the whole point of escalating.
        """
        return self.candidates[0][0] if self.candidates else None

    def model_context(self) -> dict[str, object]:
        """The bundle, packaged for the LLM classifier.

        Deliberately built here rather than in the LLM module so that what the
        model sees is defined by the classifier that gave up, not by whoever
        writes the prompt. PII redaction happens downstream, on the way out of
        the process.
        """
        return {
            "reason": self.reason,
            "raw_code": self.signals.raw_code,
            "gateway_description": self.signals.gateway_description,
            "bank_narration": self.signals.bank_narration,
            "rail": normalise_rail(self.signals.rail_hint),
            "deterministic_candidates": [
                {"failure_class": fc.value, "confidence_bps": bps} for fc, bps in self.candidates
            ],
            "evidence": [e.describe() for e in self.evidence],
            "allowed_values": [fc.value for fc in FailureClass],
        }


ClassificationResult = DeterministicClassification | UnresolvedClassification


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def classify_signals(signals: ClassificationSignals) -> ClassificationResult:
    """Classify a whole signal bundle, or say plainly that it cannot be done.

    The scoring rule, in full: each class takes the weight of its strongest
    single piece of evidence, plus a bonus for every *additional* field that
    independently agrees. The leader must clear
    :data:`RESOLVE_THRESHOLD_BPS` and beat the runner-up by
    :data:`DECISION_MARGIN_BPS`. Anything else escalates.

    Three consequences worth stating, because they are choices rather than
    accidents. A recognised code always resolves. A single free-text phrase
    never does. Two free-text phrases from different systems that agree do.
    """
    rail = normalise_rail(signals.rail_hint)
    evidence = gather_evidence(signals, rail)
    if not evidence:
        return UnresolvedClassification(reason=NO_RECOGNISED_SIGNAL, signals=signals)

    strongest: dict[FailureClass, int] = {}
    supporters: dict[FailureClass, set[str]] = {}
    for item in evidence:
        strongest[item.failure_class] = max(strongest.get(item.failure_class, 0), item.weight_bps)
        supporters.setdefault(item.failure_class, set()).add(item.field_name)

    scored = {
        failure_class: min(
            10000, weight + CORROBORATION_BONUS_BPS * (len(supporters[failure_class]) - 1)
        )
        for failure_class, weight in strongest.items()
    }
    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0].value))
    best_class, best_score = ranked[0]
    runner_up_score = ranked[1][1] if len(ranked) > 1 else 0

    if best_score < RESOLVE_THRESHOLD_BPS:
        return UnresolvedClassification(
            reason=WEAK_EVIDENCE,
            signals=signals,
            evidence=evidence,
            candidates=tuple(ranked),
        )
    if best_score - runner_up_score < DECISION_MARGIN_BPS:
        return UnresolvedClassification(
            reason=CONFLICTING_SIGNALS,
            signals=signals,
            evidence=evidence,
            candidates=tuple(ranked),
        )

    primary = min(
        (e for e in evidence if e.failure_class is best_class),
        key=lambda e: (-e.weight_bps, _FIELD_ORDER.index(e.field_name), e.token),
    )
    return DeterministicClassification(
        failure_class=best_class,
        confidence_bps=best_score,
        matched_code=primary.token,
        matched_namespace=primary.namespace,
        matched_field=primary.field_name,
        evidence=evidence,
        conflicting=tuple(e for e in evidence if e.failure_class is not best_class),
    )


def classify_failure(
    *,
    raw_code: str | None = None,
    gateway_description: str | None = None,
    bank_narration: str | None = None,
    rail_hint: str | AuthorisationType | None = None,
) -> ClassificationResult:
    """Keyword convenience wrapper over :func:`classify_signals`.

    Callers assembling a bundle from a webhook payload have four loose strings,
    not a dataclass; making them build one first adds nothing.
    """
    return classify_signals(
        ClassificationSignals(
            raw_code=raw_code,
            gateway_description=gateway_description,
            bank_narration=bank_narration,
            rail_hint=rail_hint,
        )
    )
