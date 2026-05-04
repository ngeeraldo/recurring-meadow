# Recurring Meadow

End-to-end MRR reporting pipeline backed by Stripe test data.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in STRIPE_API_KEY=sk_test_...
```

## Smoke test

The smoke test creates a single customer "6 months ago" via a Stripe Test Clock, charges an initial invoice with a working card, swaps in a card that fails on collection, and advances the clock 31 days so the first renewal fails. It then polls until the subscription transitions to `past_due`. Use it to validate that the SDK plumbing works end-to-end before scaling up to the full seeder.

```bash
python -m scripts.smoke_test
```

After it completes, the Stripe Dashboard (test mode) should show:
- 1 customer
- 1 subscription with status `past_due`
- 1 paid invoice (initial) and 1 open/failed renewal invoice

> Note: if you advance much further past the failure, Stripe's Smart Retries will exhaust and your account's "if all retries fail" setting (typically *Cancel subscription*) will run — the sub will end up `canceled` instead of `past_due`. The smoke test stops at +31 days for this reason.

To wipe the test data after a run, use **Stripe Dashboard → Developers → Delete all test data**.

## Tests

```bash
pytest
```
