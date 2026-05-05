from __future__ import annotations

import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import stripe

from . import catalog, config, run_log, stripe_client, simulator


@dataclass
class CustomerState:
    """The interface between simulator results and Stripe-side state.

    Created by ``event_handler._create_customer_in_stripe``, mutated in
    place by handlers as the simulation replays, read by ``run()`` for the
    catch-up loop and final summary.
    """
    sim_id: str
    stripe_customer_id: str
    sub_id: str
    clock_id: str
    clock_day: int            # latest sim-day the Stripe clock has reached
    current_tier: str
    current_cadence: str
    current_state: str = "active"


# Imported AFTER CustomerState is defined to break the circular dependency
# (event_handler imports CustomerState lazily, but listing the import here
# documents the runtime relationship).
from . import event_handler  # noqa: E402


def _build_price_catalog() -> dict:
    out = {}
    for tier in config.TIER_LADDER:
        for cadence in ("month", "year"):
            price = catalog.get_or_create_price(tier, cadence)
            out[(tier, cadence)] = price.id
    return out


def run() -> None:
    stripe_client.init()

    print("Provisioning price catalog (4 tiers × 2 cadences)...")
    price_map = _build_price_catalog()
    print(f"  → {len(price_map)} prices ready")

    print(
        f"Running simulator (seed={config.RNG_SEED}, "
        f"days={config.SIMULATION_DAYS}, "
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
        event_handler.handle_event(event, states, price_map, base_frozen_time)

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
            event_handler.advance_clock_to(state, config.SIMULATION_DAYS, base_frozen_time)
        else:
            print(f"  {sim_id}: already at day {state.clock_day}, no advance needed")

    elapsed = time.monotonic() - started
    print()
    print(f"Replay + catch-up complete in {elapsed:.1f}s.")
    print()

    # Refresh from Stripe — local state can drift during catch-up (Smart
    # Retries can auto-cancel past_due subs without us hearing about it).
    print("Collecting final state from Stripe for run log...")
    state_counts: Counter = Counter()
    tier_counts: Counter = Counter()
    cadence_counts: Counter = Counter()
    for s in states.values():
        try:
            live = stripe.Subscription.retrieve(s.sub_id)
            actual = live.status
        except stripe.error.InvalidRequestError:
            actual = "unretrievable"
        state_counts[actual] += 1
        if actual == "active":
            tier_counts[s.current_tier] += 1
            cadence_counts[s.current_cadence] += 1

    initial_count = sum(
        1 for e in events if e.type == "customer_created" and e.day == 0
    )
    log_text = run_log.format_run_log(
        seed=config.RNG_SEED,
        sim_days=config.SIMULATION_DAYS,
        initial_count=initial_count,
        events=events,
        states=states,
        state_counts=state_counts,
        tier_counts=tier_counts,
        cadence_counts=cadence_counts,
        generated_on=datetime.now(timezone.utc).date().isoformat(),
    )
    log_path = Path(__file__).resolve().parents[2] / "output" / "seeder_events.txt"
    log_path.write_text(log_text)
    print(f"Run log written to {log_path}")
    print()
    print('Cleanup: Stripe Dashboard → Developers → "Delete all test data"')


if __name__ == "__main__":
    run()
    sys.exit(0)
