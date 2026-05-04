from unittest.mock import MagicMock, patch

from scripts import customers


def test_create_customer_attaches_test_clock():
    created = MagicMock(id="cus_abc")
    with patch("stripe.Customer.create", return_value=created) as create:
        result = customers.create_customer(clock_id="clock_xyz", email="a@b.test")

    create.assert_called_once_with(test_clock="clock_xyz", email="a@b.test")
    assert result is created


def test_create_payment_method_uses_token_directly():
    pm = MagicMock(id="pm_card_visa")
    with patch("stripe.PaymentMethod.retrieve", return_value=pm) as retrieve:
        result = customers.create_payment_method(token="pm_card_visa")

    retrieve.assert_called_once_with("pm_card_visa")
    assert result is pm


def test_create_payment_method_from_card_number():
    pm = MagicMock(id="pm_new")
    with patch("stripe.PaymentMethod.create", return_value=pm) as create:
        result = customers.create_payment_method(card_number="4000000000000341")

    create.assert_called_once()
    kwargs = create.call_args.kwargs
    assert kwargs["type"] == "card"
    assert kwargs["card"]["number"] == "4000000000000341"
    assert result is pm


def test_attach_payment_method_sets_default_when_requested():
    with patch("stripe.PaymentMethod.attach") as attach, \
         patch("stripe.Customer.modify") as modify:
        customers.attach_payment_method("cus_abc", "pm_xyz", set_default=True)

    attach.assert_called_once_with("pm_xyz", customer="cus_abc")
    modify.assert_called_once_with(
        "cus_abc",
        invoice_settings={"default_payment_method": "pm_xyz"},
    )


def test_attach_payment_method_skips_default_when_not_requested():
    with patch("stripe.PaymentMethod.attach") as attach, \
         patch("stripe.Customer.modify") as modify:
        customers.attach_payment_method("cus_abc", "pm_xyz", set_default=False)

    attach.assert_called_once_with("pm_xyz", customer="cus_abc")
    modify.assert_not_called()
