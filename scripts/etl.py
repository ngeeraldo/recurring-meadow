"""ETL: Stripe → BigQuery.

Extracts customers, subscriptions, and invoice line items from Stripe and
loads them into the BigQuery `stripe_raw` dataset (drop-and-recreate per
run). Step 3 of the project consumes this dataset to compute MRR via SQL.

Schema favors simplicity over normalization per the take-home spec —
denormalized columns repeat customer_id on every line item, and nested
subscription items live in a single JSON column rather than a child table.

Auth + config (all read from .env):
- ``STRIPE_API_KEY`` — Stripe secret key.
- ``BIGQUERY_PROJECT`` — GCP project id that owns the dataset.
- ``BIGQUERY_DB`` — BigQuery dataset name (defaults to ``stripe_raw``).
- BigQuery uses Application Default Credentials. Run
  ``gcloud auth application-default login`` once.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import stripe
from dotenv import load_dotenv
from google.cloud import bigquery

from scripts.seeder import stripe_client

# Load .env so BIGQUERY_PROJECT / BIGQUERY_DB can live alongside STRIPE_API_KEY.
# override=True so .env wins over a stale value lingering in the shell env
# (e.g. from a previous `export BIGQUERY_PROJECT=...`).
load_dotenv(override=True)

PROJECT_ID = os.environ.get("BIGQUERY_PROJECT", "")
DATASET_ID = os.environ.get("BIGQUERY_DB", "stripe_raw")
_PLACEHOLDER_PROJECT_IDS = {"your-gcp-project-id", "your-default-project-id", ""}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(epoch: Optional[int]) -> Optional[str]:
    """Convert a Unix epoch (seconds) to an ISO-8601 string for BigQuery."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _serialize_for_json(value: Any) -> str:
    """Serialize a Stripe SDK object (or list of them) to a JSON string.

    Stripe v15.x ``StripeObject`` does NOT subclass dict, so ``json.dumps``
    can't walk it natively — it falls back to ``default=str``, which calls
    the SDK's pretty-printed JSON repr and then escapes the whole thing
    as a string. The result is double-encoded garbage.

    Use the SDK's public ``to_dict()`` to get a recursive plain-dict
    representation first, then ``json.dumps`` produces clean output.
    """
    if isinstance(value, list):
        plain = [v.to_dict() if hasattr(v, "to_dict") else v for v in value]
    else:
        plain = value.to_dict() if hasattr(value, "to_dict") else value
    return json.dumps(plain, default=str)


def _all_customers() -> Iterator[stripe.Customer]:
    """Yield every customer, including those attached to test clocks.

    ``Customer.list`` excludes test-clock customers by default, so we
    enumerate clocks first and de-dupe by id.
    """
    seen: set = set()
    for clock in stripe.test_helpers.TestClock.list().auto_paging_iter():
        for cus in stripe.Customer.list(test_clock=clock.id).auto_paging_iter():
            if cus.id not in seen:
                seen.add(cus.id)
                yield cus
    for cus in stripe.Customer.list().auto_paging_iter():
        if cus.id not in seen:
            seen.add(cus.id)
            yield cus


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------

def extract_customers() -> list:
    print("Extracting customers from Stripe...")
    rows = []
    for cus in _all_customers():
        rows.append({
            "id": cus.id,
            "email": cus.email,
            "created": _ts(cus.created),
        })
    print(f"  Loaded {len(rows)} customers")
    return rows


def _resolve_current_period(sub, items) -> tuple:
    """Return (current_period_start, current_period_end) Unix epochs.

    Newer Stripe API versions moved these fields from the Subscription
    object to the SubscriptionItem level, so we fall back to the first
    item when the sub-level field is missing.
    NB: stripe-python 15.x StripeObject is NOT a dict subclass — we use
    ``getattr(obj, key, default)`` because ``obj.get(...)`` raises
    AttributeError.
    """
    start = getattr(sub, "current_period_start", None)
    end = getattr(sub, "current_period_end", None)
    if start is None and items:
        start = getattr(items[0], "current_period_start", None)
    if end is None and items:
        end = getattr(items[0], "current_period_end", None)
    return start, end


def _subscription_to_row(sub) -> dict:
    """Flatten a Stripe Subscription into a BigQuery row dict."""
    items = list(sub["items"].data)
    period_start, period_end = _resolve_current_period(sub, items)
    return {
        "id": sub.id,
        "customer_id": sub.customer,
        "status": sub.status,
        "start_date": _ts(getattr(sub, "start_date", None)),
        "current_period_start": _ts(period_start),
        "current_period_end": _ts(period_end),
        "canceled_at": _ts(getattr(sub, "canceled_at", None)),
        "items": _serialize_for_json(items),
    }


def extract_subscriptions() -> list:
    # Same test-clock gotcha as customers: Subscription.list with no filter
    # silently omits subs whose customer is attached to a test clock. Iterate
    # per customer to pull everything.
    print("Extracting subscriptions from Stripe...")
    rows = []
    for cus in _all_customers():
        for sub in stripe.Subscription.list(
            customer=cus.id,
            status="all",
            expand=["data.items.data.price"],
        ).auto_paging_iter():
            rows.append(_subscription_to_row(sub))
    print(f"  Loaded {len(rows)} subscriptions")
    return rows


def _build_price_interval_map() -> dict:
    """Map ``price_id → recurring.interval`` (e.g. ``"month"``, ``"year"``).

    Stripe's newer API doesn't include the full Price object in invoice line
    items by default — line.pricing.price_details.price is just the price id.
    We list all prices once and look up the interval per line item.
    """
    out = {}
    for price in stripe.Price.list(limit=100).auto_paging_iter():
        recurring = getattr(price, "recurring", None)
        out[price.id] = getattr(recurring, "interval", None) if recurring else None
    return out


def _line_subscription_id(line) -> Optional[str]:
    """Subscription id for a subscription-item line (newer API path)."""
    parent = getattr(line, "parent", None)
    sub_details = getattr(parent, "subscription_item_details", None) if parent else None
    return getattr(sub_details, "subscription", None) if sub_details else None


def _invoice_subscription_id(inv) -> Optional[str]:
    """Fallback: invoice-level subscription id.

    The old ``inv.subscription`` field was moved to
    ``inv.parent.subscription_details.subscription`` in the newer API.
    """
    parent = getattr(inv, "parent", None)
    sub_details = getattr(parent, "subscription_details", None) if parent else None
    return getattr(sub_details, "subscription", None) if sub_details else None


def _line_proration(line) -> Optional[bool]:
    parent = getattr(line, "parent", None)
    sub_details = getattr(parent, "subscription_item_details", None) if parent else None
    return getattr(sub_details, "proration", None) if sub_details else None


def _line_pricing(line) -> tuple:
    """Return ``(price_id, unit_amount)`` from ``line.pricing``.

    ``unit_amount_decimal`` comes back as a string (e.g. ``"1500"``); cast to int.
    """
    pricing = getattr(line, "pricing", None)
    if pricing is None:
        return None, None
    price_details = getattr(pricing, "price_details", None)
    price_id = getattr(price_details, "price", None) if price_details else None
    unit_decimal = getattr(pricing, "unit_amount_decimal", None)
    unit_amount = int(float(unit_decimal)) if unit_decimal is not None else None
    return price_id, unit_amount


def _invoice_line_item_to_row(line, inv, price_intervals: dict) -> dict:
    """Flatten one invoice line into a BigQuery row dict (newer API paths)."""
    period = getattr(line, "period", None)
    period_start = getattr(period, "start", None) if period else None
    period_end = getattr(period, "end", None) if period else None

    price_id, unit_amount = _line_pricing(line)
    interval = price_intervals.get(price_id) if price_id else None

    # Prefer the line-level subscription id; fall back to the invoice-level one.
    subscription_id = _line_subscription_id(line) or _invoice_subscription_id(inv)

    return {
        "line_item_id": line.id,
        "invoice_id": inv.id,
        "invoice_status": inv.status,
        "customer_id": inv.customer,
        "subscription_id": subscription_id,
        "period_start": _ts(period_start),
        "period_end": _ts(period_end),
        "amount": line.amount,
        "currency": line.currency,
        "interval": interval,
        "proration": _line_proration(line),
        "quantity": getattr(line, "quantity", None),
        "unit_amount": unit_amount,
    }


def extract_invoice_line_items() -> list:
    # Same test-clock gotcha — iterate per customer.
    print("Extracting invoices and flattening line items from Stripe...")
    print("  Building price → interval map...")
    price_intervals = _build_price_interval_map()
    print(f"    {len(price_intervals)} prices indexed")

    rows = []
    inv_count = 0
    for cus in _all_customers():
        for inv in stripe.Invoice.list(customer=cus.id).auto_paging_iter():
            inv_count += 1
            for line in inv.lines.data:
                rows.append(_invoice_line_item_to_row(line, inv, price_intervals))
    print(f"  Read {inv_count} invoices, loaded {len(rows)} invoice line items")
    return rows


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

CUSTOMERS_SCHEMA = [
    bigquery.SchemaField("id", "STRING"),
    bigquery.SchemaField("email", "STRING"),
    bigquery.SchemaField("created", "TIMESTAMP"),
]

SUBSCRIPTIONS_SCHEMA = [
    bigquery.SchemaField("id", "STRING"),
    bigquery.SchemaField("customer_id", "STRING"),
    bigquery.SchemaField("status", "STRING"),
    bigquery.SchemaField("start_date", "TIMESTAMP"),
    bigquery.SchemaField("current_period_start", "TIMESTAMP"),
    bigquery.SchemaField("current_period_end", "TIMESTAMP"),
    bigquery.SchemaField("canceled_at", "TIMESTAMP"),
    bigquery.SchemaField("items", "JSON"),
]

INVOICE_LINE_ITEMS_SCHEMA = [
    bigquery.SchemaField("line_item_id", "STRING"),
    bigquery.SchemaField("invoice_id", "STRING"),
    bigquery.SchemaField("invoice_status", "STRING"),
    bigquery.SchemaField("customer_id", "STRING"),
    bigquery.SchemaField("subscription_id", "STRING"),
    bigquery.SchemaField("period_start", "TIMESTAMP"),
    bigquery.SchemaField("period_end", "TIMESTAMP"),
    bigquery.SchemaField("amount", "INT64"),
    bigquery.SchemaField("currency", "STRING"),
    bigquery.SchemaField("interval", "STRING"),
    bigquery.SchemaField("proration", "BOOL"),
    bigquery.SchemaField("quantity", "INT64"),
    bigquery.SchemaField("unit_amount", "INT64"),
]


def _ensure_dataset(client: bigquery.Client) -> None:
    dataset_ref = f"{PROJECT_ID}.{DATASET_ID}"
    try:
        client.get_dataset(dataset_ref)
    except Exception:
        print(f"Creating dataset {dataset_ref}...")
        client.create_dataset(bigquery.Dataset(dataset_ref))


def _load(
    client: bigquery.Client,
    table: str,
    rows: list,
    schema: list,
) -> None:
    table_ref = f"{PROJECT_ID}.{DATASET_ID}.{table}"
    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    job = client.load_table_from_json(rows, table_ref, job_config=job_config)
    job.result()
    print(f"  Wrote {len(rows)} rows → {table}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run() -> None:
    if PROJECT_ID in _PLACEHOLDER_PROJECT_IDS:
        print(
            f"BIGQUERY_PROJECT is unset or still a placeholder ({PROJECT_ID!r}). "
            "Set the real project id in .env. If you also exported "
            "BIGQUERY_PROJECT in your shell, unset it so .env wins.",
            file=sys.stderr,
        )
        sys.exit(2)

    stripe_client.init()
    bq = bigquery.Client(project=PROJECT_ID)
    _ensure_dataset(bq)

    customers = extract_customers()
    subscriptions = extract_subscriptions()
    line_items = extract_invoice_line_items()

    print()
    print(f"Loading into {PROJECT_ID}.{DATASET_ID}...")
    _load(bq, "customers", customers, CUSTOMERS_SCHEMA)
    _load(bq, "subscriptions", subscriptions, SUBSCRIPTIONS_SCHEMA)
    _load(bq, "invoice_line_items", line_items, INVOICE_LINE_ITEMS_SCHEMA)

    print()
    print(f"ETL complete. Tables ready in {PROJECT_ID}.{DATASET_ID}.")


if __name__ == "__main__":
    run()
    sys.exit(0)
