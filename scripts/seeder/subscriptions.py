import time

import stripe

from . import customers

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
    pm = customers.create_payment_method(token=WORKING_CARD_TOKEN)
    customers.attach_payment_method(customer_id, pm.id, set_default=True)
    sub = stripe.Subscription.retrieve(sub_id)
    stripe.Invoice.pay(sub.latest_invoice)
    return stripe.Subscription.retrieve(sub_id)


def cancel_subscription(sub_id: str) -> stripe.Subscription:
    return stripe.Subscription.cancel(sub_id)


def change_tier(sub_id: str, new_price_id: str) -> stripe.Subscription:
    sub = stripe.Subscription.retrieve(sub_id)
    item_id = sub["items"].data[0].id
    return stripe.Subscription.modify(
        sub_id,
        items=[{"id": item_id, "price": new_price_id}],
        proration_behavior="none",
    )
