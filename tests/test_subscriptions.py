from unittest.mock import MagicMock, patch

import pytest

from scripts import subscriptions


def test_create_subscription_passes_customer_and_price():
    sub = MagicMock(id="sub_abc")
    with patch("stripe.Subscription.create", return_value=sub) as create:
        result = subscriptions.create_subscription("cus_abc", "price_xyz")

    create.assert_called_once_with(
        customer="cus_abc",
        items=[{"price": "price_xyz"}],
    )
    assert result is sub


def test_set_failing_card_creates_attaches_and_sets_default():
    pm = MagicMock(id="pm_card_chargeCustomerFail")
    with patch("scripts.subscriptions.customers.create_payment_method", return_value=pm) as cpm, \
         patch("scripts.subscriptions.customers.attach_payment_method") as attach:
        result = subscriptions.set_failing_card("cus_abc")

    cpm.assert_called_once_with(token="pm_card_chargeCustomerFail")
    attach.assert_called_once_with("cus_abc", "pm_card_chargeCustomerFail", set_default=True)
    assert result is pm


def test_wait_for_status_change_returns_when_status_differs():
    active = MagicMock(status="active")
    past_due = MagicMock(status="past_due")
    with patch("stripe.Subscription.retrieve", side_effect=[active, active, past_due]) as retrieve, \
         patch("scripts.subscriptions.time.sleep"):
        result = subscriptions.wait_for_status_change("sub_abc", from_status="active", timeout=10)

    assert retrieve.call_count == 3
    assert result is past_due


def test_wait_for_status_change_times_out():
    active = MagicMock(status="active")
    with patch("stripe.Subscription.retrieve", return_value=active), \
         patch("scripts.subscriptions.time.sleep"), \
         patch("scripts.subscriptions.time.monotonic", side_effect=[0.0, 0.5, 1.0, 1.5, 99.0]):
        with pytest.raises(TimeoutError, match="sub_abc"):
            subscriptions.wait_for_status_change("sub_abc", from_status="active", timeout=10)
