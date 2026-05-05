"""ETL: Stripe → BigQuery.

Extracts paid invoice line items from Stripe and loads them into the
``invoice_line_items`` table in the BigQuery ``stripe_raw`` dataset
(drop-and-recreate per run). Step 3 (``sql/mrr_monthly.sql``) and the
dashboard API both read from this single table.

The line-item rows are deliberately denormalized — ``customer_id``,
``subscription_id``, ``quantity``, ``unit_amount``, and ``interval`` are all
on every row so ad-hoc queries don't need joins.

Auth + config (all read from .env):
- ``STRIPE_API_KEY`` — Stripe secret key.
- ``BIGQUERY_PROJECT`` — GCP project id that owns the dataset.
- ``BIGQUERY_DB`` — BigQuery dataset name (defaults to ``stripe_raw``).
- BigQuery uses Application Default Credentials. Run
  ``gcloud auth application-default login`` once.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Iterator, Optional

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

    line_items = extract_invoice_line_items()

    print()
    print(f"Loading into {PROJECT_ID}.{DATASET_ID}...")
    _load(bq, "invoice_line_items", line_items, INVOICE_LINE_ITEMS_SCHEMA)

    print()
    print(f"ETL complete. invoice_line_items ready in {PROJECT_ID}.{DATASET_ID}.")


if __name__ == "__main__":
    run()
    sys.exit(0)
