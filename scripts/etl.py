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

from scripts import stripe_client

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
    """Serialize a Stripe SDK object (or list) to a JSON string.

    Stripe SDK objects subclass ``dict``, so ``json.dumps`` walks them
    natively. ``default=str`` is a backstop for any unexpected types.
    """
    return json.dumps(value, default=str)


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


def extract_invoice_line_items() -> list:
    # Same test-clock gotcha — iterate per customer.
    print("Extracting invoices and flattening line items from Stripe...")
    rows = []
    inv_count = 0
    for cus in _all_customers():
        for inv in stripe.Invoice.list(
            customer=cus.id,
            expand=["data.lines.data.price"],
        ).auto_paging_iter():
            inv_count += 1
            for line in inv.lines.data:
                # StripeObjects are not dicts in v15.x — use getattr(...).
                price = getattr(line, "price", None)
                interval = None
                unit_amount = None
                if price is not None:
                    recurring = getattr(price, "recurring", None)
                    if recurring is not None:
                        interval = getattr(recurring, "interval", None)
                    unit_amount = getattr(price, "unit_amount", None)

                period = getattr(line, "period", None)
                period_start = getattr(period, "start", None) if period else None
                period_end = getattr(period, "end", None) if period else None

                rows.append({
                    "line_item_id": line.id,
                    "invoice_id": inv.id,
                    "invoice_status": inv.status,
                    "customer_id": inv.customer,
                    "subscription_id": getattr(inv, "subscription", None),
                    "period_start": _ts(period_start),
                    "period_end": _ts(period_end),
                    "amount": line.amount,
                    "currency": line.currency,
                    "interval": interval,
                    "proration": getattr(line, "proration", None),
                    "quantity": getattr(line, "quantity", None),
                    "unit_amount": unit_amount,
                })
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
