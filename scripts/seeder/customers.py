from typing import Optional

import stripe


def create_customer(clock_id: str, email: str) -> stripe.Customer:
    return stripe.Customer.create(test_clock=clock_id, email=email)


def create_payment_method(
    token: Optional[str] = None,
    card_number: Optional[str] = None,
) -> stripe.PaymentMethod:
    if token and card_number:
        raise ValueError("provide either token or card_number, not both")
    if token:
        return stripe.PaymentMethod.retrieve(token)
    if card_number:
        return stripe.PaymentMethod.create(
            type="card",
            card={
                "number": card_number,
                "exp_month": 12,
                "exp_year": 2099,
                "cvc": "123",
            },
        )
    raise ValueError("provide either token or card_number")


def attach_payment_method(customer_id: str, pm_id: str, set_default: bool = True) -> None:
    stripe.PaymentMethod.attach(pm_id, customer=customer_id)
    if set_default:
        stripe.Customer.modify(
            customer_id,
            invoice_settings={"default_payment_method": pm_id},
        )
