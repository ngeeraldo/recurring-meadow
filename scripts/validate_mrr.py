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

Outputs to stdout and ``output/validation_output.txt``.

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

from scripts import etl
from scripts.seeder import catalog, config

load_dotenv(override=True)

PROJECT_ID = os.environ.get("BIGQUERY_PROJECT", "")
DATASET_ID = os.environ.get("BIGQUERY_DB", "stripe_raw")
STRIPE_API_KEY = os.environ.get("STRIPE_API_KEY", "")

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_FILE = REPO_ROOT / "sql" / "mrr_monthly.sql"
OUTPUT_FILE = REPO_ROOT / "output" / "validation_output.txt"

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

def _customer_contributions(
    customer_id: str,
    window: list,
    now_epoch: int,
) -> tuple:
    """Walk one customer's paid USD line items.

    Returns ``({month: Decimal historical_contribution}, Decimal current_mrr)``.

    Historical: half-open attribution to the calendar months in ``window``.
    Current: sum of ``amount / 100 / months_covered`` for every line whose
    period straddles ``now_epoch`` (i.e. ``start <= now < end``). This mirrors
    the ``current_mrr`` CTE in ``sql/mrr_monthly.sql``.
    """
    historical: dict = {m: Decimal(0) for m in window}
    current = Decimal(0)
    window_set = set(window)

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

            start_d = datetime.fromtimestamp(start_epoch, tz=timezone.utc).date()
            end_d = datetime.fromtimestamp(end_epoch, tz=timezone.utc).date()
            months_covered = max(_month_diff(end_d, start_d), 1)
            contribution = Decimal(amount) / Decimal(100) / Decimal(months_covered)

            # Historical attribution (half-open).
            first_covered = date(start_d.year, start_d.month, 1)
            for offset in range(months_covered):
                m = _add_months(first_covered, offset)
                if m in window_set:
                    historical[m] += contribution

            # Current MRR (line whose period spans NOW).
            if start_epoch <= now_epoch < end_epoch:
                current += contribution

    return historical, current


# ---------------------------------------------------------------------------
# BigQuery queries
# ---------------------------------------------------------------------------

def _bq_client() -> bigquery.Client:
    return bigquery.Client(project=PROJECT_ID)


def _query_bq_totals(client: bigquery.Client) -> tuple:
    """Run sql/mrr_monthly.sql.

    Returns ``(historical, current)`` where:
    - ``historical`` is a dict ``{month: Decimal mrr_amount}`` for rows with
      ``is_current = FALSE``.
    - ``current`` is a tuple ``(month, Decimal mrr_amount)`` for the single
      ``is_current = TRUE`` row, or ``None`` if missing.
    """
    sql = SQL_FILE.read_text()
    rows = list(client.query(sql).result())
    historical: dict = {}
    current = None
    for row in rows:
        amount = Decimal(str(row.mrr_amount))
        if getattr(row, "is_current", False):
            current = (row.month, amount)
        else:
            historical[row.month] = amount
    return historical, current


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


_PER_CUSTOMER_CURRENT_SQL = """
SELECT
  customer_id,
  ROUND(SUM(amount / 100.0
    / GREATEST(DATE_DIFF(DATE(period_end), DATE(period_start), MONTH), 1)
  ), 2) AS bq_amount
FROM `{dataset}.invoice_line_items`
WHERE invoice_status = 'paid'
  AND currency = 'usd'
  AND period_start <= CURRENT_TIMESTAMP()
  AND period_end > CURRENT_TIMESTAMP()
GROUP BY customer_id
"""


def _query_bq_per_customer_current(client: bigquery.Client) -> dict:
    """{customer_id: Decimal current_mrr} — used only when the current row fails.

    Mirrors the ``current_mrr`` CTE in sql/mrr_monthly.sql but with
    ``customer_id`` in the GROUP BY for a per-customer breakdown.
    """
    sql = _PER_CUSTOMER_CURRENT_SQL.format(dataset=DATASET_ID)
    rows = list(client.query(sql).result())
    return {row.customer_id: Decimal(str(row.bq_amount)) for row in rows}


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

def _run_sanity_check(bq_historical: dict, fh) -> bool:
    """Compare analytical projection to BQ historical totals. ±20% tolerance.

    Only historical months are checked here. The current row is a snapshot
    of subscriptions billing right now; comparing it against a full-month
    analytical average isn't a meaningful sanity check, so it's excluded.
    Stage 2 still validates the current row exactly.
    """
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
        bq_val = bq_historical.get(m, Decimal(0))
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


def _emit_per_customer_breakdown(
    label: str,
    py_amounts: dict,
    bq_amounts: dict,
    fh,
) -> None:
    """Print a single per-customer table comparing Python and BQ amounts.

    ``py_amounts`` and ``bq_amounts`` are flat ``{customer_id: Decimal}``
    dicts. Customers with zero on both sides are dropped; the remainder
    is sorted by ``|delta|`` descending and rows over the validation
    tolerance are flagged.
    """
    _emit("", fh)
    _emit(f"=== Per-customer breakdown for {label} ===", fh)
    _emit(
        f"  {'Customer':<32} {'BigQuery':>12} {'Python':>12} {'Delta':>12}",
        fh,
    )

    cus_ids = set(py_amounts.keys()) | set(bq_amounts.keys())
    rows = []
    for cus_id in cus_ids:
        py_v = py_amounts.get(cus_id, Decimal(0))
        bq_v = bq_amounts.get(cus_id, Decimal(0))
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


def _run_validation_report(
    bq_historical: dict,
    bq_current,
    python_totals: dict,
    python_current_total: Decimal,
    per_customer_python: dict,
    per_customer_python_current: dict,
    bq_client_: bigquery.Client,
    fh,
) -> bool:
    """Compare per-customer Python walk to BQ totals. Returns True if all rows pass."""
    window = _window_months()

    _emit("", fh)
    _emit("=" * 70, fh)
    _emit("Stage 2 — MRR Validation Report (Python walk vs BigQuery, ±$0.01)", fh)
    _emit("=" * 70, fh)
    _emit("", fh)
    _emit(
        f"  {'Month':<16} {'BigQuery SQL':>14} {'Python Valid.':>16}"
        f" {'Delta':>12}    Status",
        fh,
    )

    failures = []      # historical months that failed
    current_failed = False
    for m in window:
        py = python_totals[m]
        bq_val = bq_historical.get(m, Decimal(0))
        delta = py - bq_val
        status = "PASS" if abs(delta) < VALIDATION_TOLERANCE else "FAIL"
        if status == "FAIL":
            failures.append(m)
        _emit(
            f"  {m.isoformat():<16} {_money(bq_val):>14} {_money(py):>16}"
            f" {_money(delta):>12}    {status}",
            fh,
        )

    if bq_current is not None:
        cur_month, cur_bq = bq_current
        delta = python_current_total - cur_bq
        status = "PASS" if abs(delta) < VALIDATION_TOLERANCE else "FAIL"
        if status == "FAIL":
            current_failed = True
        label = f"{cur_month.isoformat()} (now)"
        _emit(
            f"  {label:<16} {_money(cur_bq):>14} {_money(python_current_total):>16}"
            f" {_money(delta):>12}    {status}",
            fh,
        )

    _emit("", fh)
    total_rows = len(window) + (1 if bq_current is not None else 0)
    if not failures and not current_failed:
        _emit(f"Stage 2 passed. ({total_rows} rows within ${VALIDATION_TOLERANCE})", fh)
        return True

    fail_labels = [m.isoformat() for m in failures]
    if current_failed:
        fail_labels.append(f"{bq_current[0].isoformat()} (now)")
    _emit(f"Stage 2 failed for {len(fail_labels)} row(s): {fail_labels}", fh)

    # Per-customer breakdown for any failing historical months
    if failures:
        _emit("", fh)
        _emit("Pulling per-customer historical breakdown from BigQuery...", fh)
        bq_per_customer = _query_bq_per_customer(bq_client_)

        for failed_month in failures:
            _emit_per_customer_breakdown(
                label=failed_month.isoformat(),
                py_amounts={
                    c: per_customer_python.get(c, {}).get(failed_month, Decimal(0))
                    for c in per_customer_python
                },
                bq_amounts={
                    c: bq_per_customer.get(c, {}).get(failed_month, Decimal(0))
                    for c in bq_per_customer
                },
                fh=fh,
            )

    # Per-customer breakdown for the current row, if it failed
    if current_failed:
        _emit("", fh)
        _emit("Pulling per-customer current breakdown from BigQuery...", fh)
        bq_per_customer_cur = _query_bq_per_customer_current(bq_client_)

        cur_month = bq_current[0]
        _emit_per_customer_breakdown(
            label=f"{cur_month.isoformat()} (now)",
            py_amounts=per_customer_python_current,
            bq_amounts=bq_per_customer_cur,
            fh=fh,
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
        bq_historical, bq_current = _query_bq_totals(bq)
        cur_label = f" + 1 current ({bq_current[0].isoformat()})" if bq_current else ""
        _emit(f"  got {len(bq_historical)} historical months{cur_label} from BigQuery", fh)
        _emit("", fh)

        # --- Stage 1: sanity check (historical only) -----------------------
        sanity_ok = _run_sanity_check(bq_historical, fh)

        # --- Walk customers in Python (slower; only needed for stage 2) -----
        _emit("", fh)
        _emit("Walking customer invoices in Python (for stage 2)...", fh)
        window = _window_months()
        now_epoch = int(datetime.now(tz=timezone.utc).timestamp())
        python_totals: dict = {m: Decimal(0) for m in window}
        python_current_total = Decimal(0)
        per_customer_python: dict = {}
        per_customer_python_current: dict = {}
        cus_count = 0
        for cus in etl._all_customers():
            cus_count += 1
            historical, current = _customer_contributions(cus.id, window, now_epoch)
            per_customer_python[cus.id] = historical
            per_customer_python_current[cus.id] = current
            for m, v in historical.items():
                python_totals[m] += v
            python_current_total += current
        _emit(f"  walked {cus_count} customers", fh)

        # --- Stage 2: validation report -------------------------------------
        validation_ok = _run_validation_report(
            bq_historical,
            bq_current,
            python_totals,
            python_current_total,
            per_customer_python,
            per_customer_python_current,
            bq,
            fh,
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
