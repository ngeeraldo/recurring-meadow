"""Pure-Python stochastic simulator for the seeder.

Walks ``SIMULATION_DAYS`` days. On each day, every existing customer rolls
dice for state transitions, and a small number of Bernoulli trials decide
whether a new customer is acquired. The output is a chronological event log
that the seeder replays against Stripe.

No Stripe SDK calls — this is fully unit-testable and deterministic when
called with a fixed seed.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Optional

from scripts import config


@dataclass
class SimCustomer:
    sim_id: str
    tier: str           # one of config.TIER_LADDER
    cadence: str        # "month" or "year"
    state: str          # "active" / "past_due" / "canceled"
    created_day: int    # 0 for the initial roster


@dataclass
class Event:
    day: int
    sim_id: str
    type: str
    payload: dict = field(default_factory=dict)


def _weighted_choice(rng: random.Random, weights: dict) -> str:
    return rng.choices(list(weights.keys()), weights=list(weights.values()), k=1)[0]


def _make_customer(rng: random.Random, sim_id: str, created_day: int) -> SimCustomer:
    return SimCustomer(
        sim_id=sim_id,
        tier=_weighted_choice(rng, config.INITIAL_TIER_WEIGHTS),
        cadence=_weighted_choice(rng, config.INITIAL_CADENCE_WEIGHTS),
        state="active",
        created_day=created_day,
    )


def _adjacent_tier(current: str, direction: int) -> Optional[str]:
    """Return the tier one step up (direction=+1) or down (-1), or None at the boundary."""
    idx = config.TIER_LADDER.index(current)
    new_idx = idx + direction
    if 0 <= new_idx < len(config.TIER_LADDER):
        return config.TIER_LADDER[new_idx]
    return None


def _roll_active(cust: SimCustomer, rng: random.Random, day: int, events: list) -> None:
    p = config.FROM_ACTIVE
    roll = rng.random()
    cum = 0.0

    cum += p["past_due"]
    if roll < cum:
        cust.state = "past_due"
        events.append(Event(day=day, sim_id=cust.sim_id, type="marked_past_due"))
        return

    cum += p["canceled"]
    if roll < cum:
        cust.state = "canceled"
        events.append(Event(day=day, sim_id=cust.sim_id, type="canceled"))
        return

    cum += p["upgrade"]
    if roll < cum:
        new_tier = _adjacent_tier(cust.tier, +1)
        if new_tier is not None:
            events.append(Event(
                day=day, sim_id=cust.sim_id, type="tier_upgraded",
                payload={"from": cust.tier, "to": new_tier},
            ))
            cust.tier = new_tier
        # else: at the ceiling — stay put, no event
        return

    cum += p["downgrade"]
    if roll < cum:
        new_tier = _adjacent_tier(cust.tier, -1)
        if new_tier is not None:
            events.append(Event(
                day=day, sim_id=cust.sim_id, type="tier_downgraded",
                payload={"from": cust.tier, "to": new_tier},
            ))
            cust.tier = new_tier
        # else: at the floor — stay put, no event
        return

    # else: stay active, same tier


def _roll_past_due(cust: SimCustomer, rng: random.Random, day: int, events: list) -> None:
    p = config.FROM_PAST_DUE
    roll = rng.random()
    cum = 0.0

    cum += p["active"]
    if roll < cum:
        cust.state = "active"
        events.append(Event(day=day, sim_id=cust.sim_id, type="recovered"))
        return

    cum += p["canceled"]
    if roll < cum:
        cust.state = "canceled"
        events.append(Event(day=day, sim_id=cust.sim_id, type="canceled"))
        return


def _roll_canceled(cust: SimCustomer, rng: random.Random, day: int, events: list) -> None:
    p = config.FROM_CANCELED
    if rng.random() < p["active"]:
        cust.state = "active"
        events.append(Event(day=day, sim_id=cust.sim_id, type="recovered"))


def simulate(seed: Optional[int] = None) -> tuple:
    """Run the full simulation. Returns (roster, events) in chronological order.

    ``roster`` includes all customers ever created (initial + acquired).
    ``events`` is sorted by day (initial creates at day 0 come first).
    """
    rng_seed = seed if seed is not None else config.RNG_SEED
    rng = random.Random(rng_seed)

    roster: list = []
    events: list = []

    # Day 0: initial roster.
    for i in range(config.INITIAL_CUSTOMER_COUNT):
        cust = _make_customer(rng, sim_id=f"sim_{i}", created_day=0)
        roster.append(cust)
        events.append(Event(
            day=0, sim_id=cust.sim_id, type="customer_created",
            payload={"tier": cust.tier, "cadence": cust.cadence},
        ))

    # Day-by-day.
    for day in range(1, config.SIMULATION_DAYS + 1):
        # Use a snapshot so newly-acquired customers don't roll on their birth day.
        for cust in list(roster):
            if cust.state == "active":
                _roll_active(cust, rng, day, events)
            elif cust.state == "past_due":
                _roll_past_due(cust, rng, day, events)
            elif cust.state == "canceled":
                _roll_canceled(cust, rng, day, events)

        # Acquisition: N independent Bernoulli rolls.
        for _ in range(config.ACQUISITION_ROLLS_PER_DAY):
            if rng.random() < config.ACQUISITION_P_PER_ROLL:
                cust = _make_customer(
                    rng, sim_id=f"sim_{len(roster)}", created_day=day,
                )
                roster.append(cust)
                events.append(Event(
                    day=day, sim_id=cust.sim_id, type="customer_created",
                    payload={"tier": cust.tier, "cadence": cust.cadence},
                ))

    return roster, events
