"""Seeder orchestrator: simulate, then replay against Stripe.

Runs the pure-Python simulator in scripts/simulator.py, then walks the
resulting event log in chronological order, dispatching each event to the
appropriate Stripe helper.

Cleanup: Stripe Dashboard → Developers → "Delete all test data".
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import stripe

from scripts import (
    catalog,
    clocks,
    config,
    customers,
    simulator,
    stripe_client,
    subscriptions,
)


@dataclass
class CustomerState:
    sim_id: str
    stripe_customer_id: str
    sub_id: str
    clock_id: str
    clock_day: int            # latest sim-day the Stripe clock has reached
    current_tier: str
    current_cadence: str
    current_state: str = "active"


def _frozen_for_day(base_frozen_time: int, sim_day: int) -> int:
    return base_frozen_time + sim_day * 86_400


def _build_price_catalog() -> dict:
    out = {}
    for tier in config.TIER_LADDER:
        for cadence in ("month", "year"):
            price = catalog.get_or_create_price(tier, cadence)
            out[(tier, cadence)] = price.id
    return out


def _create_customer_in_stripe(
    sim_id: str,
    tier: str,
    cadence: str,
    created_day: int,
    base_frozen_time: int,
    price_map: dict,
) -> CustomerState:
    clock = clocks.create_clock(
        frozen_time=_frozen_for_day(base_frozen_time, created_day)
    )
    cus = customers.create_customer(
        clock_id=clock.id, email=f"seed+{sim_id}@test.local",
    )
    pm = customers.create_payment_method(token="pm_card_visa")
    customers.attach_payment_method(cus.id, pm.id, set_default=True)
    sub = subscriptions.create_subscription(cus.id, price_map[(tier, cadence)])
    return CustomerState(
        sim_id=sim_id,
        stripe_customer_id=cus.id,
        sub_id=sub.id,
        clock_id=clock.id,
        clock_day=created_day,
        current_tier=tier,
        current_cadence=cadence,
        current_state="active",
    )


# Stripe allows advancing a test clock by at most 2 of the shortest billing
# interval per call. For monthly subs that's 2 calendar months — which can be
# as short as 59 days (Jan→Mar). 56 stays comfortably inside that ceiling.
# For yearly subs the ceiling is 2 years, so 720 days is well within.
_MAX_HOP_DAYS = {"month": 56, "year": 720}


def _advance_clock_to(
    state: CustomerState, target_day: int, base_frozen_time: int,
) -> None:
    # Hard cap: a Stripe Test Clock should never advance past today.
    target_day = min(target_day, config.SIMULATION_DAYS)
    if target_day <= state.clock_day:
        return
    max_hop = _MAX_HOP_DAYS[state.current_cadence]
    while state.clock_day < target_day:
        next_day = min(state.clock_day + max_hop, target_day)
        clocks.advance_clock(
            state.clock_id,
            frozen_time=_frozen_for_day(base_frozen_time, next_day),
        )
        state.clock_day = next_day


def _handle_event(
    event: simulator.Event,
    states: dict,
    price_map: dict,
    base_frozen_time: int,
) -> None:
    if event.type == "customer_created":
        if event.sim_id not in states:
            states[event.sim_id] = _create_customer_in_stripe(
                sim_id=event.sim_id,
                tier=event.payload["tier"],
                cadence=event.payload["cadence"],
                created_day=event.day,
                base_frozen_time=base_frozen_time,
                price_map=price_map,
            )
        return

    state = states.get(event.sim_id)
    if state is None:
        return  # event for unknown customer; shouldn't happen

    # Stripe's actual state can drift from our local tracking — most notably,
    # Smart Retries can auto-cancel a past_due sub during a clock advance.
    # Refresh from Stripe before each transition.
    try:
        live = stripe.Subscription.retrieve(state.sub_id)
        state.current_state = live.status
    except stripe.error.InvalidRequestError:
        print(f"      (sub {state.sub_id} unretrievable — skip)")
        return

    if event.type in ("tier_upgraded", "tier_downgraded"):
        if state.current_state in ("canceled", "incomplete_expired"):
            print(f"      (sub is {state.current_state}, cannot tier-change — skip)")
            return
        _advance_clock_to(state, event.day, base_frozen_time)
        new_tier = event.payload["to"]
        subscriptions.change_tier(
            state.sub_id, price_map[(new_tier, state.current_cadence)],
        )
        state.current_tier = new_tier

    elif event.type == "marked_past_due":
        if state.current_state != "active":
            print(f"      (sub is {state.current_state}, not active — skip)")
            return
        # The +31d advance below is what actually fires the renewal failure
        # in Stripe. If that would cross past today (SIMULATION_DAYS), skip
        # the event — we'd rather under-report past_due than fabricate
        # future-dated Stripe data.
        needed_day = event.day + 31
        if needed_day > config.SIMULATION_DAYS:
            print(f"      (need day {needed_day} > {config.SIMULATION_DAYS} to fire renewal — skip)")
            return
        _advance_clock_to(state, event.day, base_frozen_time)
        subscriptions.set_failing_card(state.stripe_customer_id)
        # +31d to fire the next renewal failure.
        _advance_clock_to(state, state.clock_day + 31, base_frozen_time)
        subscriptions.wait_for_status_change(state.sub_id, from_status="active")
        state.current_state = "past_due"

    elif event.type == "recovered":
        if state.current_state == "past_due":
            subscriptions.recover_from_past_due(
                state.stripe_customer_id, state.sub_id,
            )
            subscriptions.wait_for_status_change(
                state.sub_id, from_status="past_due",
            )
            state.current_state = "active"
        else:
            # canceled, active, or anything else — nothing to recover from.
            print(f"      (sub is {state.current_state}, nothing to recover — skip)")

    elif event.type == "canceled":
        if state.current_state == "canceled":
            print("      (sub already canceled in Stripe — skip)")
            return
        _advance_clock_to(state, event.day, base_frozen_time)
        try:
            subscriptions.cancel_subscription(state.sub_id)
        except stripe.error.InvalidRequestError as e:
            # Dunning during the advance above can auto-cancel the sub.
            # Stripe's cancel-on-already-canceled error is the misleading
            # "No such subscription" — treat it as success since the sub
            # ended up where we wanted it anyway.
            if "No such subscription" not in str(e):
                raise
            print("      (sub auto-canceled during clock advance — treating as success)")
        state.current_state = "canceled"


def run() -> None:
    stripe_client.init()

    print("Provisioning price catalog (4 tiers × 2 cadences)...")
    price_map = _build_price_catalog()
    print(f"  → {len(price_map)} prices ready")

    print(
        f"Running simulator (seed={config.RNG_SEED}, "
        f"days={config.SIMULATION_DAYS}, "
        f"initial={config.INITIAL_CUSTOMER_COUNT})..."
    )
    roster, events = simulator.simulate(seed=config.RNG_SEED)
    print(f"  → {len(roster)} customers, {len(events)} events")

    now = datetime.now(timezone.utc)
    base_frozen_time = int((now - timedelta(days=config.SIMULATION_DAYS)).timestamp())
    print(
        f"Replaying against Stripe "
        f"(base frozen_time = {datetime.fromtimestamp(base_frozen_time, tz=timezone.utc).isoformat()})..."
    )

    states: dict = {}
    started = time.monotonic()

    for i, event in enumerate(events):
        print(f"  [{i+1:>3}/{len(events)}] day={event.day:>3} {event.type:<18} {event.sim_id}")
        _handle_event(event, states, price_map, base_frozen_time)

    # Catch up every clock to "now" so customers without events still
    # generate their full renewal/invoice history through the simulation
    # window. Without this, a customer who never had an event would be
    # frozen at their creation day and produce no invoices.
    print()
    print(f"Catching all clocks up to day {config.SIMULATION_DAYS}...")
    for sim_id in sorted(states.keys()):
        state = states[sim_id]
        if state.clock_day < config.SIMULATION_DAYS:
            print(f"  {sim_id}: day {state.clock_day} → {config.SIMULATION_DAYS}")
            _advance_clock_to(state, config.SIMULATION_DAYS, base_frozen_time)
        else:
            print(f"  {sim_id}: already at day {state.clock_day}, no advance needed")

    elapsed = time.monotonic() - started
    print()
    print(f"Replay + catch-up complete in {elapsed:.1f}s.")
    print()

    # Refresh status from Stripe for an accurate summary (long catch-up
    # advances may have triggered renewals or auto-cancellations).
    state_counts: Counter = Counter()
    tier_counts: Counter = Counter()
    for s in states.values():
        try:
            live = stripe.Subscription.retrieve(s.sub_id)
            actual = live.status
        except stripe.error.InvalidRequestError:
            actual = "unretrievable"
        state_counts[actual] += 1
        if actual == "active":
            tier_counts[s.current_tier] += 1

    print(f"Final state distribution: {dict(state_counts)}")
    print(f"Final active-tier distribution: {dict(tier_counts)}")
    print()
    print('Cleanup: Stripe Dashboard → Developers → "Delete all test data"')


if __name__ == "__main__":
    run()
    sys.exit(0)
