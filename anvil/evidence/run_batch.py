"""Runnable batch experiment. ``python -m anvil.evidence.run_batch``

Wiring only. Everything it calls is tested elsewhere; keeping this module a
small, obvious function is what makes it easy to see that the numbers in the
report came from the components they claim to.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys

import structlog

from anvil.evidence.assignment import DEFAULT_SPLIT, EVEN_SPLIT
from anvil.evidence.metrics import as_json, summarise
from anvil.evidence.report import render
from anvil.simulator.population import build_population
from anvil.simulator.world import World

#: The batch's own reference instant. Passed in rather than read from the clock
#: so a run in December reproduces a run in September exactly.
BATCH_EPOCH = dt.datetime(2026, 9, 1, 6, 0, tzinfo=dt.UTC)


def run(
    *,
    seed: int,
    size: int,
    even_split: bool,
    model_available: bool,
    horizon_days: int,
) -> tuple[str, dict[str, object]]:
    """Build the world, work every arm, and return the report and its JSON."""
    population = build_population(seed=seed, size=size, now=BATCH_EPOCH)
    world = World(
        population,
        horizon_days=horizon_days,
        split=EVEN_SPLIT if even_split else DEFAULT_SPLIT,
        model_available=model_available,
    )
    outcomes = world.run_batch()
    summary = summarise(outcomes, seed=seed, population_size=size)
    return render(summary, model_available=model_available), as_json(summary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="anvil.evidence.run_batch",
        description="Run the seeded recovery batch and print the evidence report.",
    )
    parser.add_argument("--seed", type=int, default=20260902)
    parser.add_argument("--size", type=int, default=3000, help="subscriptions in the book")
    parser.add_argument(
        "--split",
        choices=("even", "production"),
        default="even",
        help="even gives each arm a third, which is what makes the intervals tight enough "
        "to conclude anything; production holds back 10%% for control and 10%% for baseline",
    )
    parser.add_argument(
        "--with-model",
        action="store_true",
        help="model the LLM classifier as available, so its contribution can be measured",
    )
    parser.add_argument("--horizon-days", type=int, default=30)
    parser.add_argument("--json", type=str, default=None, help="also write the JSON here")
    args = parser.parse_args(argv)

    # The batch is a report, not a service: the degradation warnings each case
    # emits are expected and would drown the output.
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))

    report, payload = run(
        seed=args.seed,
        size=args.size,
        even_split=args.split == "even",
        model_available=args.with_model,
        horizon_days=args.horizon_days,
    )
    print(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        print(f"\nJSON written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
