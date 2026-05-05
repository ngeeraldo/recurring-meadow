"""Two-stage cross-check of ``sql/mrr_monthly.sql``.

**Stage 1 — Sanity check.** Compute an analytical MRR projection per month
from ``catalog.PLANS`` + ``scripts/config.py`` (closed-form solution to
``dA/dt = new − k·A``) and compare to the BigQuery total within **±20%**.
Catches order-of-magnitude regressions: wrong tier weights, broken churn
rates, missing customers, etc.

**Stage 2 — MRR Validation Report.** Walk every customer's paid USD invoice
line items in Python with the same period-spreading logic as the SQL, and
compare to the BigQuery total within **$0.01**. Same source data, same
methodology, fundamentally different code structure — agreement validates
the SQL aggregation's internal consistency.

Outputs to stdout and ``validation_output.txt`` at the project root.

Auth (read from ``.env``, mirrors ``scripts/etl.py``):
- ``STRIPE_API_KEY``
- ``BIGQUERY_PROJECT``

Run with:
    python -m scripts.validate_mrr
"""
from __future__ import annotations

import math
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import stripe
from dotenv import load_dotenv
from google.cloud import bigquery

from scripts import catalog, config, etl

load_dotenv(override=True)

PROJECT_ID = os.environ.get("BIGQUERY_PROJECT", "")
DATASET_ID = os.environ.get("BIGQUERY_DB", "stripe_raw")
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_FILE = REPO_ROOT / "sql" / "mrr_monthly.sql"
OUTPUT_FILE = REPO_ROOT / "validation_output.txt"

# Validation window: Nov 2025 .. Apr 2026 inclusive.
WINDOW_START = date(2025, 11, 1)
WINDOW_END = date(2026, 4, 1)

VALIDATION_TOLERANCE = Decimal("0.01")    # Stage 2: cent-level agreement
SANITY_TOLERANCE_PCT = Decimal("20")      # Stage 1: ±20%

PLACEHOLDERS = {"", "your-gcp-project-id", "your-default-project-id"}


# ---------------------------------------------------------------------------
# Date math (pure-Python equivalents of BigQuery DATE_DIFF / DATE_ADD MONTH)
# ---------------------------------------------------------------------------

def _month_diff(end_d: date, start_d: date) -> int:
    """Number of MONTH boundaries between two dates — matches BQ DATE_DIFF(.., MONTH)."""
    return (end_d.year - start_d.year) * 12 + (end_d.month - start_d.month)


def _add_months(d: date, n: int) -> date:
    """Add n months. Always returns the first of the resulting month."""
    total = d.month - 1 + n
    return date(d.year + total // 12, total % 12 + 1, 1)


def _window_months() -> list:
    """Inclusive list of first-of-month dates from WINDOW_START to WINDOW_END."""
    months = []
    cur = WINDOW_START
    while cur <= WINDOW_END:
        months.append(cur)
        cur = _add_months(cur, 1)
    return months


# ---------------------------------------------------------------------------
# Stage 1: analytical projection (used by sanity check)
# ---------------------------------------------------------------------------

def _expected_per_subscriber_mrr_cents() -> float:
    """Weighted average monthly contribution across tier × cadence.

    Yearly cadence is normalized to a monthly figure via
    ``catalog.PLANS[tier]["annual_effective_cents"]``.
    """
    total = 0.0
    for tier, p_tier in config.INITIAL_TIER_WEIGHTS.items():
        plan = catalog.PLANS[tier]
        for cadence, p_cadence in config.INITIAL_CADENCE_WEIGHTS.items():
            rate = plan["monthly_cents"] if cadence == "month" else plan["annual_effective_cents"]
            total += p_tier * p_cadence * rate
    return total


def _effective_monthly_churn() -> float:
    """Direct cancel + (past_due → eventually canceled), as a monthly rate."""
    direct = config.FROM_ACTIVE["canceled"] * 30
    to_past_due = config.FROM_ACTIVE["past_due"] * 30

    pd_to_active = config.FROM_PAST_DUE["active"] * 30
    pd_to_canceled = config.FROM_PAST_DUE["canceled"] * 30
    pd_exit = pd_to_active + pd_to_canceled
    p_pd_canceled = pd_to_canceled / pd_exit if pd_exit else 0.0

    return direct + to_past_due * p_pd_canceled


def _expected_active_at_month(month: int, k: float, A0: float, new: float) -> float:
    """A(t) = A_ss + (A(0) − A_ss) · exp(−k·t)."""
    A_ss = new / k if k > 0 else float("inf")
    return A_ss + (A0 - A_ss) * math.exp(-k * month)


def _expected_mrr_for_months(num_months: int) -> list:
    """Expected MRR (Decimal dollars) for months +1 .. +num_months."""
    per_sub = _expected_per_subscriber_mrr_cents() / 100
    k = _effective_monthly_churn()
    new = config.NEW_CUSTOMERS_PER_MONTH_AVG
    A0 = float(config.INITIAL_CUSTOMER_COUNT)
    return [
        Decimal(str(_expected_active_at_month(t, k, A0, new) * per_sub))
        for t in range(1, num_months + 1)
    ]


# ---------------------------------------------------------------------------
# Stage 2: per-customer Python walk
# ---------------------------------------------------------------------------

def _attribute_line_to_months(
    period_start_epoch: int,
    period_end_epoch: int,
    amount_cents: int,
    window: list,
) -> dict:
    """Half-open attribution of one paid line item to its covered months.

    Mirrors the SQL exactly:
        months_covered = max(month_diff(end, start), 1)
        contribution    = amount/100 / months_covered
        attribute to [first_covered_month, first_covered_month + months_covered MONTH)
    """
    start_d = datetime.fromtimestamp(period_start_epoch, tz=timezone.utc).date()
    end_d = datetime.fromtimestamp(period_end_epoch, tz=timezone.utc).date()
    months_covered = max(_month_diff(end_d, start_d), 1)

    contribution = Decimal(amount_cents) / Decimal(100) / Decimal(months_covered)
    first_covered = date(start_d.year, start_d.month, 1)

    window_set = set(window)
    out: dict = {}
    for offset in range(months_covered):
        m = _add_months(first_covered, offset)
        if m in window_set:
            out[m] = out.get(m, Decimal(0)) + contribution
    return out


def _customer_monthly_contributions(customer_id: str, window: list) -> dict:
    """Walk one customer's paid USD line items and return {month: Decimal contribution}."""
    contribs: dict = {m: Decimal(0) for m in window}
    for inv in stripe.Invoice.list(
        customer=customer_id,
        expand=["data.lines.data.price"],
    ).auto_paging_iter():
        if getattr(inv, "status", None) != "paid":
            continue
        for line in inv.lines.data:
            if getattr(line, "currency", None) != "usd":
                continue
            period = getattr(line, "period", None)
            if period is None:
                continue
            start_epoch = getattr(period, "start", None)
            end_epoch = getattr(period, "end", None)
            amount = getattr(line, "amount", None)
            if start_epoch is None or end_epoch is None or amount is None:
                continue
            line_attr = _attribute_line_to_months(start_epoch, end_epoch, amount, window)
            for m, v in line_attr.items():
                contribs[m] += v
    return contribs


# ---------------------------------------------------------------------------
# BigQuery queries
# ---------------------------------------------------------------------------

def _bq_client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT_ID)


def _query_bq_totals(client: bigquery.Client) -> dict:
    """Run sql/mrr_monthly.sql; return {month: Decimal mrr_amount}."""
    sql = SQL_FILE.read_text()
    rows = list(client.query(sql).result())
    return {row.month: Decimal(str(row.mrr_amount)) for row in rows}


_PER_CUSTOMER_SQL = f"""
WITH
  month_series AS (
    SELECT m AS month
    FROM UNNEST(GENERATE_DATE_ARRAY(
      DATE '{WINDOW_START.isoformat()}', DATE '{WINDOW_END.isoformat()}', INTERVAL 1 MONTH
    )) AS m
  ),
  line_items AS (
    SELECT
      customer_id,
      CAST(amount AS NUMERIC) / 100
        / GREATEST(DATE_DIFF(DATE(period_end), DATE(period_start), MONTH), 1)
        AS monthly_contribution,
      DATE_TRUNC(DATE(period_start), MONTH) AS first_covered_month,
      GREATEST(DATE_DIFF(DATE(period_end), DATE(period_start), MONTH), 1) AS months_covered
    FROM `{{dataset}}.invoice_line_items`
    WHERE invoice_status = 'paid' AND currency = 'usd'
  )
SELECT
  ms.month,
  li.customer_id,
  ROUND(SUM(li.monthly_contribution), 2) AS bq_amount
FROM month_series AS ms
LEFT JOIN line_items AS li
  ON ms.month >= li.first_covered_month
 AND ms.month <  DATE_ADD(li.first_covered_month, INTERVAL li.months_covered MONTH)
WHERE li.customer_id IS NOT NULL
GROUP BY ms.month, li.customer_id
"""


def _query_bq_per_customer(client: bigquery.Client) -> dict:
    """{customer_id: {month: Decimal bq_amount}} — used only when validation fails."""
    sql = _PER_CUSTOMER_SQL.format(dataset=DATASET_ID)
    rows = list(client.query(sql).result())
    out: dict = {}
    for row in rows:
        out.setdefault(row.customer_id, {})[row.month] = Decimal(str(row.bq_amount))
    return out


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _emit(line: str, fh=None) -> None:
    print(line)
    if fh is not None:
        fh.write(line + "\n")


def _money(d: Decimal) -> str:
    sign = "-" if d < 0 else " "
    return f"{sign}${abs(d):>9,.2f}"


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------

def _run_sanity_check(bq_totals: dict, fh) -> bool:
    """Compare analytical projection to BQ totals. Returns True if all months pass."""
    window = _window_months()
    expected = _expected_mrr_for_months(num_months=len(window))

    _emit("=" * 70, fh)
    _emit("Stage 1 — Sanity check (analytical projection vs BigQuery, ±20%)", fh)
    _emit("=" * 70, fh)
    _emit("", fh)
    _emit(
        f"  {'Month':<12} {'Expected':>12} {'BigQuery':>12} {'Δ%':>8}    Status",
        fh,
    )

    failures = []
    for m, exp in zip(window, expected):
        bq_val = bq_totals.get(m, Decimal(0))
        pct = ((bq_val - exp) / exp * 100) if exp > 0 else Decimal(0)
        status = "PASS" if abs(pct) <= SANITY_TOLERANCE_PCT else "FAIL"
        if status == "FAIL":
            failures.append(m)
        _emit(
            f"  {m.isoformat():<12} {_money(exp):>12} {_money(bq_val):>12}"
            f" {pct:>+7.1f}%      {status}",
            fh,
        )

    _emit("", fh)
    if not failures:
        _emit(f"Stage 1 passed. ({len(window)} months within ±{SANITY_TOLERANCE_PCT}%)", fh)
        return True
    _emit(
        f"Stage 1 failed for {len(failures)} month(s): "
        f"{[m.isoformat() for m in failures]}",
        fh,
    )
    return False


def _run_validation_report(
    bq_totals: dict,
    python_totals: dict,
    per_customer_python: dict,
    bq_client_: bigquery.Client,
    fh,
) -> bool:
    """Compare per-customer Python walk to BQ totals. Returns True if all months pass."""
    window = _window_months()

    _emit("", fh)
    _emit("=" * 70, fh)
    _emit("Stage 2 — MRR Validation Report (Python walk vs BigQuery, ±$0.01)", fh)
    _emit("=" * 70, fh)
    _emit("", fh)
    _emit(
        f"  {'Month':<12} {'BigQuery SQL':>14} {'Python Valid.':>16}"
        f" {'Delta':>12}    Status",
        fh,
    )

    failures = []
    for m in window:
        py = python_totals[m]
        bq_val = bq_totals.get(m, Decimal(0))
        delta = py - bq_val
        status = "PASS" if abs(delta) < VALIDATION_TOLERANCE else "FAIL"
        if status == "FAIL":
            failures.append(m)
        _emit(
            f"  {m.isoformat():<12} {_money(bq_val):>14} {_money(py):>16}"
            f" {_money(delta):>12}    {status}",
            fh,
        )

    _emit("", fh)
    if not failures:
        _emit(f"Stage 2 passed. ({len(window)} months within ${VALIDATION_TOLERANCE})", fh)
        return True

    _emit(
        f"Stage 2 failed for {len(failures)} month(s): "
        f"{[m.isoformat() for m in failures]}",
        fh,
    )
    _emit("", fh)
    _emit("Pulling per-customer breakdown from BigQuery...", fh)
    bq_per_customer = _query_bq_per_customer(bq_client_)

    for failed_month in failures:
        _emit("", fh)
        _emit(f"=== Per-customer breakdown for {failed_month.isoformat()} ===", fh)
        _emit(
            f"  {'Customer':<32} {'BigQuery':>12} {'Python':>12} {'Delta':>12}",
            fh,
        )

        cus_ids = set(per_customer_python.keys()) | set(bq_per_customer.keys())
        rows = []
        for cus_id in cus_ids:
            py_v = per_customer_python.get(cus_id, {}).get(failed_month, Decimal(0))
            bq_v = bq_per_customer.get(cus_id, {}).get(failed_month, Decimal(0))
            if py_v == 0 and bq_v == 0:
                continue
            rows.append((cus_id, bq_v, py_v, py_v - bq_v))
        rows.sort(key=lambda r: abs(r[3]), reverse=True)

        for cus_id, bq_v, py_v, delta in rows:
            marker = "  ← divergent" if abs(delta) >= VALIDATION_TOLERANCE else ""
            _emit(
                f"  {cus_id:<32} {_money(bq_v):>12} {_money(py_v):>12}"
                f" {_money(delta):>12}{marker}",
                fh,
            )

    return False


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def validate() -> int:
    if PROJECT_ID in PLACEHOLDERS:
        print(
            f"BIGQUERY_PROJECT is unset or a placeholder ({PROJECT_ID!r}). Set it in .env.",
            file=sys.stderr,
        )
        return 2
    if STRIPE_API_KEY in PLACEHOLDERS:
        print("STRIPE_API_KEY is unset or a placeholder. Set it in .env.", file=sys.stderr)
        return 2
    stripe.api_key = STRIPE_API_KEY

    fh = OUTPUT_FILE.open("w")
    try:
        _emit("MRR Validation — multi-stage cross-check of sql/mrr_monthly.sql", fh)
        _emit(f"Generated: {datetime.now(timezone.utc).isoformat()}", fh)
        _emit("", fh)

        # --- Pull BigQuery totals (used by both stages) ----------------------
        _emit("Querying BigQuery sql/mrr_monthly.sql...", fh)
        bq = _bq_client()
        bq_totals = _query_bq_totals(bq)
        _emit(f"  got {len(bq_totals)} months from BigQuery", fh)
        _emit("", fh)

        # --- Stage 1: sanity check ------------------------------------------
        sanity_ok = _run_sanity_check(bq_totals, fh)

        # --- Walk customers in Python (slower; only needed for stage 2) -----
        _emit("", fh)
        _emit("Walking customer invoices in Python (for stage 2)...", fh)
        window = _window_months()
        python_totals: dict = {m: Decimal(0) for m in window}
        per_customer_python: dict = {}
        cus_count = 0
        for cus in etl._all_customers():
            cus_count += 1
            contribs = _customer_monthly_contributions(cus.id, window)
            per_customer_python[cus.id] = contribs
            for m, v in contribs.items():
                python_totals[m] += v
        _emit(f"  walked {cus_count} customers", fh)

        # --- Stage 2: validation report -------------------------------------
        validation_ok = _run_validation_report(
            bq_totals, python_totals, per_customer_python, bq, fh,
        )

        _emit("", fh)
        _emit("=" * 70, fh)
        if sanity_ok and validation_ok:
            _emit("All checks passed.", fh)
            rc = 0
        else:
            failed = []
            if not sanity_ok:
                failed.append("sanity check")
            if not validation_ok:
                failed.append("validation report")
            _emit(f"Failures: {', '.join(failed)}", fh)
            rc = 1
        _emit(f"Report saved to {OUTPUT_FILE}", fh)
        return rc
    finally:
        fh.close()


if __name__ == "__main__":
    sys.exit(validate())
