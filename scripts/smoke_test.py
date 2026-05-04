"""End-to-end smoke test for the Stripe seeding pipeline.

`run()` creates a customer 6 months in the past, pays one initial invoice
with a working card, swaps in a card that fails on collection, and advances
the test clock just past the first renewal so the subscription lands in
`past_due`.

Cleanup is done via the Stripe Dashboard's "Delete all test data" option
(Developers → Delete all test data).

Why we stop right after the first failure:
- Stripe Smart Retries run after a failed renewal. Once the retry window
  (~3 weeks of simulated time) elapses, the account's "if all retries fail"
  setting kicks in -- typically `Cancel subscription`. Advancing too far past
  the failure makes the subscription land in `canceled`, not `past_due`.
"""
import sys
from datetime import datetime, timedelta, timezone

from scripts import catalog, clocks, customers, stripe_client, subscriptions

HISTORY_DAYS = 180
# Advance just past the first renewal boundary so the renewal fires but
# Stripe's Smart Retries haven't had time to exhaust and cancel the sub.
ADVANCE_DAYS = 31


def run() -> None:
    stripe_client.init()

    print("Provisioning Standard monthly price...")
    price = catalog.get_or_create_price("standard", "month")

    now = datetime.now(timezone.utc)
    frozen_time = int((now - timedelta(days=HISTORY_DAYS)).timestamp())
    print(f"Creating test clock at {datetime.fromtimestamp(frozen_time, tz=timezone.utc).isoformat()}...")
    clock = clocks.create_clock(frozen_time=frozen_time)

    print(f"Creating customer attached to clock {clock.id}...")
    customer = customers.create_customer(clock_id=clock.id, email="smoke+standard@test.local")

    print("Attaching successful payment method (pm_card_visa) for the initial invoice...")
    success_pm = customers.create_payment_method(token="pm_card_visa")
    customers.attach_payment_method(customer.id, success_pm.id, set_default=True)

    print("Creating Standard monthly subscription (initial invoice charged immediately)...")
    sub = subscriptions.create_subscription(customer.id, price.id)
    print(f"  → subscription {sub.id} status: {sub.status}")

    print("Swapping default PM to pm_card_chargeCustomerFail BEFORE advancing the clock...")
    subscriptions.set_failing_card(customer.id)

    advance_to = frozen_time + ADVANCE_DAYS * 86_400
    print(f"Advancing clock +{ADVANCE_DAYS} days (first renewal should fail)...")
    clocks.advance_clock(clock.id, frozen_time=advance_to)

    print("Polling subscription until it transitions out of 'active'...")
    final_sub = subscriptions.wait_for_status_change(sub.id, from_status="active")

    print()
    print("Smoke run complete.")
    print(f"  customer:     {customer.id}")
    print(f"  subscription: {final_sub.id} (status={final_sub.status})")
    print(f"  test clock:   {clock.id}")
    if final_sub.status != "past_due":
        print(f"  ⚠ expected past_due, observed {final_sub.status} — investigate dunning settings")


if __name__ == "__main__":
    run()
    sys.exit(0)
