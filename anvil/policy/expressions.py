"""The condition language: a small JSON expression tree, not code.

A policy rule has to be two contradictory things at once. It has to be
expressive enough that a merchant's actual intent survives compilation, and
inert enough that a language model authoring one cannot reach anything. The
resolution is a tree of tagged objects with a fixed operator set: it cannot
call, cannot loop, cannot import, cannot allocate unboundedly, and cannot see
anything except the fact catalogue in :mod:`anvil.policy.facts`.

Evaluation is pure, total and side-effect free. The same tree over the same
facts yields the same boolean on every machine, forever, which is what lets a
persisted :class:`~anvil.db.models.policy.PolicyEvaluation` be replayed rather
than merely believed.

The single most important property here is that a malformed tree **raises**.
The tempting alternative -- treat anything unparseable as "did not match" -- is
catastrophic in a fail-closed system: a typo in a DENY rule would silently turn
it off, and the resulting behaviour is indistinguishable from a rule that was
correctly evaluated and correctly did not fire. Every structural doubt is
therefore an exception, and callers are expected to treat an exception as a
denial plus an alarm, never as a pass.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from anvil.core.errors import ValidationError
from anvil.policy.facts import FACT_SPECS, FactKind, FactSpec

#: An expression node. Kept as a plain dict so it round-trips through JSONB
#: unchanged and can be diffed and hashed without a codec in the middle.
Expression = dict[str, Any]

LOGICAL_OPS: Final[frozenset[str]] = frozenset({"and", "or", "not"})
CONSTANT_OPS: Final[frozenset[str]] = frozenset({"always", "never"})
EQUALITY_OPS: Final[frozenset[str]] = frozenset({"eq", "ne"})
ORDERED_OPS: Final[frozenset[str]] = frozenset({"lt", "lte", "gt", "gte"})
MEMBERSHIP_OPS: Final[frozenset[str]] = frozenset({"in", "not_in"})
RANGE_OPS: Final[frozenset[str]] = frozenset({"between"})
COMPARISON_OPS: Final[frozenset[str]] = (
    EQUALITY_OPS | ORDERED_OPS | MEMBERSHIP_OPS | RANGE_OPS
)
ALL_OPS: Final[frozenset[str]] = LOGICAL_OPS | CONSTANT_OPS | COMPARISON_OPS

#: Structural budgets. A rule tree is a human-scale artifact; anything past
#: these limits is either a mistake or an attempt to make evaluation expensive,
#: and both deserve the same refusal.
MAX_DEPTH: Final[int] = 12
MAX_NODES: Final[int] = 256

_EXPECTED_KEYS: Final[Mapping[str, frozenset[str]]] = {
    **{op: frozenset({"op", "args"}) for op in ("and", "or")},
    "not": frozenset({"op", "arg"}),
    **{op: frozenset({"op"}) for op in CONSTANT_OPS},
    **{op: frozenset({"op", "field", "value"}) for op in COMPARISON_OPS},
}


class MalformedExpression(ValidationError):
    """A rule that cannot be trusted to mean anything.

    Carries the path to the offending node (``$.args[1].value``) because the
    first question a merchant asks about a rejected policy is *which bit*.
    """

    code = "malformed_policy_expression"

    def __init__(self, message: str, *, path: str = "$") -> None:
        super().__init__(f"{message} (at {path})", path=path)
        self.path = path


@dataclass(slots=True)
class _Budget:
    nodes: int = 0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_expression(node: object, *, path: str = "$") -> None:
    """Raise :class:`MalformedExpression` unless ``node`` is a well-formed tree.

    Called at rule construction, at compile time, and again at the top of every
    evaluation. Trees are tiny, so validating twice costs nothing and removes
    the possibility of an unvalidated tree reaching the interpreter by some
    route nobody thought of.
    """
    _validate(node, path, 0, _Budget())


def _validate(node: object, path: str, depth: int, budget: _Budget) -> None:
    budget.nodes += 1
    if budget.nodes > MAX_NODES:
        raise MalformedExpression(f"expression has more than {MAX_NODES} nodes", path=path)
    if depth > MAX_DEPTH:
        raise MalformedExpression(f"expression nests deeper than {MAX_DEPTH}", path=path)
    if not isinstance(node, Mapping):
        raise MalformedExpression(
            f"expected an object, got {type(node).__name__}", path=path
        )

    keys = set(node.keys())
    if not all(isinstance(key, str) for key in keys):
        raise MalformedExpression("object keys must be strings", path=path)

    op = node.get("op")
    if not isinstance(op, str):
        raise MalformedExpression("node has no string 'op'", path=path)
    if op not in ALL_OPS:
        raise MalformedExpression(
            f"unknown operator {op!r}; known operators are {sorted(ALL_OPS)}", path=path
        )

    expected = _EXPECTED_KEYS[op]
    if keys != expected:
        raise MalformedExpression(
            f"operator {op!r} takes exactly {sorted(expected)}, got {sorted(keys)}", path=path
        )

    if op in ("and", "or"):
        args = node["args"]
        if not isinstance(args, Sequence) or isinstance(args, str | bytes) or not args:
            raise MalformedExpression(f"{op!r} needs a non-empty list of operands", path=path)
        for index, child in enumerate(args):
            _validate(child, f"{path}.args[{index}]", depth + 1, budget)
        return
    if op == "not":
        _validate(node["arg"], f"{path}.arg", depth + 1, budget)
        return
    if op in CONSTANT_OPS:
        return

    spec = _spec_for(node["field"], path)
    _validate_operand(op, spec, node["value"], f"{path}.value")


def _spec_for(field: object, path: str) -> FactSpec:
    if not isinstance(field, str):
        raise MalformedExpression("'field' must be a string", path=path)
    spec = FACT_SPECS.get(field)
    if spec is None:
        raise MalformedExpression(
            f"{field!r} is not a fact; the catalogue is {sorted(FACT_SPECS)}", path=path
        )
    return spec


def _validate_operand(op: str, spec: FactSpec, value: object, path: str) -> None:
    if op in ORDERED_OPS or op in RANGE_OPS:
        if not spec.is_ordered:
            raise MalformedExpression(
                f"{op!r} needs an integer fact; {spec.name} is {spec.kind.value}", path=path
            )
    if op in RANGE_OPS:
        bounds = value
        if (
            not isinstance(bounds, Sequence)
            or isinstance(bounds, str | bytes)
            or len(bounds) != 2
        ):
            raise MalformedExpression("'between' takes [low, high]", path=path)
        low, high = bounds[0], bounds[1]
        _validate_literal(spec, low, f"{path}[0]")
        _validate_literal(spec, high, f"{path}[1]")
        if not isinstance(low, int) or not isinstance(high, int) or low > high:
            raise MalformedExpression("'between' bounds are inverted", path=path)
        return
    if op in MEMBERSHIP_OPS:
        if not isinstance(value, Sequence) or isinstance(value, str | bytes) or not value:
            raise MalformedExpression(f"{op!r} needs a non-empty list of literals", path=path)
        for index, member in enumerate(value):
            _validate_literal(spec, member, f"{path}[{index}]")
        return
    _validate_literal(spec, value, path)


def _validate_literal(spec: FactSpec, value: object, path: str) -> None:
    """Every literal must be the shape the fact actually is.

    This is where ``eq merchant_review_first 1`` and ``eq failure_class "expired"``
    die. Both would otherwise evaluate to a perfectly quiet ``False``.
    """
    if isinstance(value, float):
        raise MalformedExpression(
            "floats are not permitted in a policy expression; use integer minor units", path=path
        )
    if value is None:
        if not spec.nullable:
            raise MalformedExpression(f"{spec.name} is never null", path=path)
        return
    if spec.kind is FactKind.BOOLEAN:
        if not isinstance(value, bool):
            raise MalformedExpression(f"{spec.name} compares against true or false", path=path)
        return
    if spec.kind is FactKind.INT:
        if isinstance(value, bool) or not isinstance(value, int):
            raise MalformedExpression(f"{spec.name} compares against an integer", path=path)
        if spec.minimum is not None and value < spec.minimum:
            raise MalformedExpression(
                f"{spec.name} is never below {spec.minimum}", path=path
            )
        if spec.maximum is not None and value > spec.maximum:
            raise MalformedExpression(
                f"{spec.name} is never above {spec.maximum}", path=path
            )
        return
    if not isinstance(value, str):
        raise MalformedExpression(f"{spec.name} compares against a string", path=path)
    if spec.allowed is not None and value not in spec.allowed:
        raise MalformedExpression(
            f"{value!r} is not a {spec.name}; allowed values are {list(spec.allowed)}", path=path
        )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate_expression(node: object, facts: Mapping[str, Any]) -> bool:
    """Evaluate a tree against a fact mapping. Pure, total, side-effect free.

    ``facts`` is the JSON form produced by
    :meth:`~anvil.policy.facts.PolicyFacts.to_json_dict` -- the same dict that
    gets persisted, so what is evaluated and what is recorded cannot diverge.
    """
    validate_expression(node)
    return _eval(node, facts)


def _eval(node: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    op = node["op"]
    if op == "and":
        return all(_eval(child, facts) for child in node["args"])
    if op == "or":
        return any(_eval(child, facts) for child in node["args"])
    if op == "not":
        return not _eval(node["arg"], facts)
    if op == "always":
        return True
    if op == "never":
        return False

    field: str = node["field"]
    if field not in facts:
        raise MalformedExpression(f"fact {field!r} is absent from the evaluated fact set")
    actual = facts[field]
    value = node["value"]

    if op == "eq":
        return _scalar_eq(actual, value)
    if op == "ne":
        return not _scalar_eq(actual, value)
    if op in MEMBERSHIP_OPS:
        contained = any(_scalar_eq(actual, member) for member in value)
        return contained if op == "in" else not contained

    left = _require_int(actual, field)
    if op == "between":
        return bool(value[0] <= left <= value[1])
    right = _require_int(value, field)
    if op == "lt":
        return left < right
    if op == "lte":
        return left <= right
    if op == "gt":
        return left > right
    return left >= right


def _scalar_eq(actual: object, expected: object) -> bool:
    """Equality that refuses to confuse ``True`` with ``1``.

    Python's ``bool`` is an ``int``, so a plain ``==`` would let a boolean fact
    match a numeric literal. Validation already forbids writing such a rule;
    this makes the interpreter safe even if a fact row is hand-edited.
    """
    if isinstance(actual, bool) or isinstance(expected, bool):
        return isinstance(actual, bool) and isinstance(expected, bool) and actual is expected
    return bool(actual == expected)


def _require_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedExpression(
            f"fact {field!r} holds {value!r}, which has no ordering", path=f"$.{field}"
        )
    return value


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def describe(node: object) -> str:
    """Render a tree as one line of English, for diffs and the console.

    A merchant approving a compiled policy is approving the tree, not the prose
    that produced it. Showing them the tree back in readable form is the only
    honest way to ask for that approval.
    """
    validate_expression(node)
    return _describe(node)


def _describe(node: Mapping[str, Any]) -> str:
    op = node["op"]
    if op in ("and", "or"):
        joined = f" {op} ".join(_describe(child) for child in node["args"])
        return joined if len(node["args"]) == 1 else f"({joined})"
    if op == "not":
        return f"not {_describe(node['arg'])}"
    if op == "always":
        return "always"
    if op == "never":
        return "never"

    field = node["field"]
    value = node["value"]
    if op == "between":
        return f"{value[0]} <= {field} <= {value[1]}"
    if op == "in":
        return f"{field} in {list(value)}"
    if op == "not_in":
        return f"{field} not in {list(value)}"
    symbol = {"eq": "==", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}[op]
    return f"{field} {symbol} {_literal(value)}"


def _literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    return repr(value) if isinstance(value, str) else str(value)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
# Hand-writing nested dicts is how typos get into a default bundle. These
# produce the same JSON the compiler emits, so the shipped rules and the
# compiled ones are the same kind of object.


def all_of(*args: Expression) -> Expression:
    """Conjunction. One operand is legal and means exactly that operand."""
    return {"op": "and", "args": list(args)}


def any_of(*args: Expression) -> Expression:
    return {"op": "or", "args": list(args)}


def negate(arg: Expression) -> Expression:
    return {"op": "not", "arg": arg}


def always() -> Expression:
    """Matches everything. The honest way to write a blanket rule."""
    return {"op": "always"}


def never() -> Expression:
    """Matches nothing. Used to park a rule without deleting its history."""
    return {"op": "never"}


def eq(field: str, value: object) -> Expression:
    return {"op": "eq", "field": field, "value": value}


def ne(field: str, value: object) -> Expression:
    return {"op": "ne", "field": field, "value": value}


def lt(field: str, value: int) -> Expression:
    return {"op": "lt", "field": field, "value": value}


def lte(field: str, value: int) -> Expression:
    return {"op": "lte", "field": field, "value": value}


def gt(field: str, value: int) -> Expression:
    return {"op": "gt", "field": field, "value": value}


def gte(field: str, value: int) -> Expression:
    return {"op": "gte", "field": field, "value": value}


def is_in(field: str, values: Sequence[object]) -> Expression:
    return {"op": "in", "field": field, "value": list(values)}


def not_in(field: str, values: Sequence[object]) -> Expression:
    return {"op": "not_in", "field": field, "value": list(values)}


def between(field: str, low: int, high: int) -> Expression:
    return {"op": "between", "field": field, "value": [low, high]}
