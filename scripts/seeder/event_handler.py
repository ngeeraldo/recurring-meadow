"""Translates simulator events into Stripe API calls.

Each event type has a handler that knows how to mutate Stripe state
to reflect that event, including handling Stripe's edge cases (dunning
auto-cancellation, status drift, etc.).

Public surface used by the orchestrator:
- ``handle_event`` — dispatch one ``simulator.Event`` against Stripe.
  Returns ``None`` on success or a short reason string when the event was
  skipped, so the orchestrator can record skips for the final run log.
- ``advance_clock_to`` — chunked clock advance with the SIMULATION_DAYS cap;
  also called by the catch-up loop in __main__.run().

``CustomerState`` lives in __main__.py per the orchestration boundary; it's
imported here lazily inside ``_create_customer_in_stripe`` to avoid a circular
import (event_handler is loaded *during* __main__'s top-level execution).
"""
from __future__ import annotations

from typing import Optional

import stripe

from . import clocks, config, customers, simulator, subscriptions

# Stripe allows advancing a test clock by at most 2 of the shortest billing
# interval per call. For monthly subs that's 2 calendar months — which can be
# as short as 59 days (Jan→Mar). 56 stays comfortably inside that ceiling.
# For yearly subs the ceiling is 2 years, so 720 days is well within.
_MAX_HOP_DAYS = {"month": 56, "year": 720}


def _frozen_for_day(base_frozen_time: int, sim_day: int) -> int:
    return base_frozen_time + sim_day * 86_400


def advance_clock_to(state, target_day: int, base_frozen_time: int) -> None:
    """Advance one customer's test clock to ``target_day`` (capped at SIMULATION_DAYS).

    Hops in chunks bounded by ``_MAX_HOP_DAYS[cadence]`` because Stripe rejects
    a single advance > 2 billing intervals.
    """
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


def _create_customer_in_stripe(
    sim_id: str,
    tier: str,
    cadence: str,
    created_day: int,
    base_frozen_time: int,
    price_map: dict,
):
    # Lazy import to avoid the circular: __main__ → event_handler → __main__.
    from .__main__ import CustomerState

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


def _skip(reason: str) -> str:
    """Print the skip reason for live console output and return it for the run log."""
    print(f"      (skipped: {reason})")
    return reason


def handle_event(
    event: simulator.Event,
    states: dict,
    price_map: dict,
    base_frozen_time: int,
) -> Optional[str]:
    """Dispatch one simulator event to Stripe.

    Returns ``None`` on success, or a short reason string when the event
    was skipped (so the orchestrator can record skips per-event for the
    final run log).
    """
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
        return None

    state = states.get(event.sim_id)
    if state is None:
        return _skip(f"unknown customer {event.sim_id}")

    # Stripe's actual state can drift from our local tracking — most notably,
    # Smart Retries can auto-cancel a past_due sub during a clock advance.
    # Refresh from Stripe before each transition.
    try:
        live = stripe.Subscription.retrieve(state.sub_id)
        state.current_state = live.status
    except stripe.error.InvalidRequestError:
        return _skip(f"sub {state.sub_id} unretrievable")

    if event.type in ("tier_upgraded", "tier_downgraded"):
        if state.current_state in ("canceled", "incomplete_expired"):
            return _skip(f"sub is {state.current_state}, cannot tier-change")
        advance_clock_to(state, event.day, base_frozen_time)
        new_tier = event.payload["to"]
        subscriptions.change_tier(
            state.sub_id, price_map[(new_tier, state.current_cadence)],
        )
        state.current_tier = new_tier
        return None

    if event.type == "marked_past_due":
        if state.current_state != "active":
            return _skip(f"sub is {state.current_state}, not active")
        # Drive the failed-renewal advance off Stripe's own period boundary
        # rather than a blind +31d. Crossing current_period_end by exactly
        # one day fires one failed payment attempt and parks the sub in
        # past_due. Smart Retries are scheduled at later clock times; since
        # we don't advance further, they never fire and Stripe never auto-
        # cancels the sub before the simulator gets a chance to recover it.
        advance_clock_to(state, event.day, base_frozen_time)
        sub = stripe.Subscription.retrieve(state.sub_id)
        renewal_day = (sub.current_period_end - base_frozen_time) // 86_400
        target_day = renewal_day + 1
        if target_day > config.SIMULATION_DAYS:
            return _skip(
                f"would advance to day {target_day} > {config.SIMULATION_DAYS} "
                f"to fire renewal"
            )
        subscriptions.set_failing_card(state.stripe_customer_id)
        advance_clock_to(state, target_day, base_frozen_time)
        subscriptions.wait_for_status_change(state.sub_id, from_status="active")
        state.current_state = "past_due"
        print(f"      (advanced to day {target_day} — past_due fired)")
        return None

    if event.type == "recovered":
        if state.current_state != "past_due":
            return _skip(f"sub is {state.current_state}, nothing to recover")
        subscriptions.recover_from_past_due(
            state.stripe_customer_id, state.sub_id,
        )
        subscriptions.wait_for_status_change(
            state.sub_id, from_status="past_due",
        )
        state.current_state = "active"
        return None

    if event.type == "canceled":
        if state.current_state == "canceled":
            return _skip("sub already canceled in Stripe")
        advance_clock_to(state, event.day, base_frozen_time)
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
        return None

    return None
