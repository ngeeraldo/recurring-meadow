from unittest.mock import MagicMock, patch

import pytest

from scripts.seeder import subscriptions


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
    with patch("scripts.seeder.subscriptions.customers.create_payment_method", return_value=pm) as cpm, \
         patch("scripts.seeder.subscriptions.customers.attach_payment_method") as attach:
        result = subscriptions.set_failing_card("cus_abc")

    cpm.assert_called_once_with(token="pm_card_chargeCustomerFail")
    attach.assert_called_once_with("cus_abc", "pm_card_chargeCustomerFail", set_default=True)
    assert result is pm


def test_wait_for_status_change_returns_when_status_differs():
    active = MagicMock(status="active")
    past_due = MagicMock(status="past_due")
    with patch("stripe.Subscription.retrieve", side_effect=[active, active, past_due]) as retrieve, \
         patch("scripts.seeder.subscriptions.time.sleep"):
        result = subscriptions.wait_for_status_change("sub_abc", from_status="active", timeout=10)

    assert retrieve.call_count == 3
    assert result is past_due


def test_wait_for_status_change_times_out():
    active = MagicMock(status="active")
    with patch("stripe.Subscription.retrieve", return_value=active), \
         patch("scripts.seeder.subscriptions.time.sleep"), \
         patch("scripts.seeder.subscriptions.time.monotonic", side_effect=[0.0, 0.5, 1.0, 1.5, 99.0]):
        with pytest.raises(TimeoutError, match="sub_abc"):
            subscriptions.wait_for_status_change("sub_abc", from_status="active", timeout=10)


def test_recover_from_past_due_swaps_pm_and_pays_invoice():
    pm = MagicMock(id="pm_card_visa")
    sub_before = MagicMock(latest_invoice="in_open")
    sub_after = MagicMock(status="active")
    with patch("scripts.seeder.subscriptions.customers.create_payment_method", return_value=pm) as cpm, \
         patch("scripts.seeder.subscriptions.customers.attach_payment_method") as attach, \
         patch("stripe.Subscription.retrieve", side_effect=[sub_before, sub_after]) as retrieve, \
         patch("stripe.Invoice.pay") as pay:
        result = subscriptions.recover_from_past_due("cus_abc", "sub_abc")

    cpm.assert_called_once_with(token="pm_card_visa")
    attach.assert_called_once_with("cus_abc", "pm_card_visa", set_default=True)
    pay.assert_called_once_with("in_open")
    assert retrieve.call_count == 2
    assert result is sub_after


def test_cancel_subscription_calls_sdk_with_id():
    canceled = MagicMock(id="sub_abc", status="canceled")
    with patch("stripe.Subscription.cancel", return_value=canceled) as cancel:
        result = subscriptions.cancel_subscription("sub_abc")

    cancel.assert_called_once_with("sub_abc")
    assert result is canceled


def test_change_tier_modifies_existing_item_with_no_proration():
    existing = MagicMock()
    existing.__getitem__ = lambda self, key: {
        "items": MagicMock(data=[MagicMock(id="si_existing")])
    }[key]
    modified = MagicMock(id="sub_abc")
    with patch("stripe.Subscription.retrieve", return_value=existing) as retrieve, \
         patch("stripe.Subscription.modify", return_value=modified) as modify:
        result = subscriptions.change_tier("sub_abc", "price_new")

    retrieve.assert_called_once_with("sub_abc")
    modify.assert_called_once_with(
        "sub_abc",
        items=[{"id": "si_existing", "price": "price_new"}],
    )
    assert result is modified
