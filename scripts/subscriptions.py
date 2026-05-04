import time

import stripe

from scripts import customers

FAILING_CARD_TOKEN = "pm_card_chargeCustomerFail"

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
