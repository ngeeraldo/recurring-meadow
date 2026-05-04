from unittest.mock import MagicMock, patch

import stripe

from scripts import catalog


def test_get_or_create_product_returns_existing():
    existing = MagicMock(id="plan_standard")
    with patch("stripe.Product.retrieve", return_value=existing) as retrieve, \
         patch("stripe.Product.create") as create:
        result = catalog.get_or_create_product("standard")

    retrieve.assert_called_once_with("plan_standard")
    create.assert_not_called()
    assert result is existing


def test_get_or_create_product_creates_when_missing():
    err = stripe.error.InvalidRequestError("not found", "id")
    created = MagicMock(id="plan_standard")
    with patch("stripe.Product.retrieve", side_effect=err), \
         patch("stripe.Product.create", return_value=created) as create:
        result = catalog.get_or_create_product("standard")

    create.assert_called_once()
    kwargs = create.call_args.kwargs
    assert kwargs["id"] == "plan_standard"
    assert kwargs["name"] == "Standard"
    assert result is created


def test_get_or_create_price_returns_existing():
    existing = MagicMock(id="price_xxx")
    listing = MagicMock(data=[existing])
    with patch("stripe.Price.list", return_value=listing) as list_call, \
         patch("stripe.Price.create") as create, \
         patch("scripts.catalog.get_or_create_product"):
        result = catalog.get_or_create_price("standard", "month")

    list_call.assert_called_once_with(lookup_keys=["standard_monthly"], limit=1)
    create.assert_not_called()
    assert result is existing


def test_get_or_create_price_creates_monthly_when_missing():
    listing = MagicMock(data=[])
    product = MagicMock(id="plan_standard")
    created = MagicMock(id="price_new")
    with patch("stripe.Price.list", return_value=listing), \
         patch("stripe.Price.create", return_value=created) as create, \
         patch("scripts.catalog.get_or_create_product", return_value=product):
        result = catalog.get_or_create_price("standard", "month")

    create.assert_called_once_with(
        lookup_key="standard_monthly",
        product="plan_standard",
        currency="usd",
        unit_amount=1000,
        recurring={"interval": "month"},
    )
    assert result is created


def test_get_or_create_price_creates_annual_with_yearly_total():
    listing = MagicMock(data=[])
    product = MagicMock(id="plan_standard")
    with patch("stripe.Price.list", return_value=listing), \
         patch("stripe.Price.create") as create, \
         patch("scripts.catalog.get_or_create_product", return_value=product):
        catalog.get_or_create_price("standard", "year")

    kwargs = create.call_args.kwargs
    assert kwargs["lookup_key"] == "standard_yearly"
    assert kwargs["unit_amount"] == 10800  # $9/mo * 12
    assert kwargs["recurring"] == {"interval": "year"}
