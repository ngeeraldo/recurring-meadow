# Recurring Meadow

End-to-end MRR reporting pipeline backed by Stripe test data.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in STRIPE_API_KEY=sk_test_...
```

## Seeder

Run the seeder to generate the full dataset that Steps 2 and 3 (BigQuery ETL + MRR SQL) will consume.

```bash
python -m scripts.seeder
```

This runs the simulator (pure-Python, deterministic via `RNG_SEED` in `scripts/config.py`) to produce a chronological event log, then replays the events against Stripe — creating customers with test clocks at the appropriate simulated dates, attaching working/failing payment methods, transitioning subscriptions through `active` / `past_due` / `canceled` / tier changes.

Defaults (set in [scripts/config.py](scripts/config.py)):

- 10 starting customers
- ~1 new customer/month average (~6 net-new across 6 months)
- 180 simulated days
- 4 tiers (Standard / Pro Plus / Engage / Enterprise), 2 cadences (monthly / annual)

Scaling up to the full 50-customer run from [refs/seeder.md](refs/seeder.md) is just a config change.

Cleanup is the same: **Stripe Dashboard → Developers → Delete all test data**.

## ETL → BigQuery

Once Stripe has data, the ETL extracts customers, subscriptions, and invoice line items and loads them into the configured BigQuery dataset (drop-and-recreate per run).

Add to your `.env`:

```
BIGQUERY_PROJECT=your-gcp-project-id
BIGQUERY_DB=stripe_raw
```

Then:

```bash
gcloud auth application-default login   # one-time, no key file in repo
python -m scripts.etl
```

Three tables get written:

- `customers` — `id`, `email`, `created`
- `subscriptions` — `id`, `customer_id`, `status`, period boundaries, `canceled_at`, `items` (JSON)
- `invoice_line_items` — denormalized one-row-per-line-item with `customer_id`, `subscription_id`, `period_start/end`, `amount`, `currency`, `interval`, `proration`, `quantity`, `unit_amount`. This is the table Step 3's MRR SQL queries.

## Tests

```bash
pytest
```

Validation: 
pytest -s tests/test_expected_metrics.py