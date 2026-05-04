import time

import stripe

from scripts import customers

FAILING_CARD_TOKEN = "pm_card_chargeCustomerFail"
WORKING_CARD_TOKEN = "pm_card_visa"

_POLL_INTERVAL_SECONDS = 1.0


def create_subscription(customer_id: str, price_id: str) -> stripe.Subscription:
    return stripe.Subscription.create(
        customer=customer_id,
        items=[{"price": price_id}],
    )


def set_failing_card(customer_id: str) -> stripe.PaymentMethod:
    pm = customers.create_payment_method(token=FAILING_CARD_TOKEN)
    customers.attach_payment_method(customer_id, pm.id, set_default=True)
    return pm


def wait_for_status_change(sub_id: str, from_status: str, timeout: float = 30.0) -> stripe.Subscription:
    """Poll a subscription until its status differs from ``from_status``.

    Stripe propagates renewal-payment results asynchronously after a test-clock
    advance returns. Polling here lets callers wait for the resulting status
    transition (e.g. ``active`` → ``past_due``) before reading state.
    """
    deadline = time.monotonic() + timeout
    while True:
        sub = stripe.Subscription.retrieve(sub_id)
        if sub.status != from_status:
            return sub
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Subscription {sub_id} did not transition from {from_status} within {timeout}s"
            )
        time.sleep(_POLL_INTERVAL_SECONDS)


def recover_from_past_due(customer_id: str, sub_id: str) -> stripe.Subscription:
    """Swap the customer's default PM to a working card and pay the open invoice.

    Stripe's Smart Retries can take days/weeks of simulated time to recover a
    past_due sub on their own (and risk hitting the cancel-after-retries
    threshold). Paying the latest invoice directly forces an immediate
    ``past_due → active`` transition.
    """
    pm = customers.create_payment_method(token=WORKING_CARD_TOKEN)
    customers.attach_payment_method(customer_id, pm.id, set_default=True)
    sub = stripe.Subscription.retrieve(sub_id)
    stripe.Invoice.pay(sub.latest_invoice)
    return stripe.Subscription.retrieve(sub_id)


def cancel_subscription(sub_id: str) -> stripe.Subscription:
    """Immediately cancel a subscription (no proration, no grace period)."""
    return stripe.Subscription.cancel(sub_id)


def change_tier(sub_id: str, new_price_id: str) -> stripe.Subscription:
    """Replace the subscription's single item price with ``new_price_id``.

    Uses ``proration_behavior="none"`` so no proration line items are
    generated — the new price applies cleanly at the next billing cycle.
    """
    sub = stripe.Subscription.retrieve(sub_id)
    item_id = sub["items"].data[0].id
    return stripe.Subscription.modify(
        sub_id,
        items=[{"id": item_id, "price": new_price_id}],
        proration_behavior="none",
    )
