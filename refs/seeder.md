# Seeder — Step 1: Data Generation

## Overview

### Goals

- Seed the Stripe test account with **50 initial customers**, each placed into one of three subscription states: **Active**, **Canceled**, or **Past Due**.
- Simulate **180 days (~6 months)** of subscription activity using Stripe **Test Clocks** to advance time.
- Each simulated day, every existing customer has a chance to transition between states, and net-new customers have a chance to be created.
- Transition and acquisition probabilities are tuned so the resulting MRR curve approximates a company experiencing **~30% year-over-year growth**.

### Growth target

30% YoY growth → roughly **2.2% net monthly growth** (since `1.022^12 ≈ 1.30`).
Net growth = new customers − churned customers, so the monthly percentages below are calibrated such that acquisition slightly outpaces churn at that rate.

### State transition model — monthly (first stab)

Probabilities reflect a healthy growth-stage SaaS company. Each row sums to 100%. These are the **per-month** odds; they'll be decomposed into per-day probabilities in a later section.

#### From Active

The Active → Active bucket is split three ways to capture tier movement (upgrades and downgrades) within the active population.

| Transition                         | Probability |
| ---------------------------------- | ----------- |
| Active → Active (same tier)        | 91%         |
| Active → Active (upgrade tier)     | 2%          |
| Active → Active (downgrade tier)   | 1%          |
| Active → Past Due                  | 3%          |
| Active → Canceled                  | 3%          |

**Tier movement rules**

- Tier ladder: `Free → Standard → Pro Plus → Engage → Enterprise`.
- Upgrades and downgrades move exactly **one tier** at a time.
- A customer already at the top (Enterprise) who rolls "upgrade" stays put; a customer at the bottom (Free) who rolls "downgrade" stays put.
- Billing cadence (monthly vs. annual) is preserved on tier transitions.

#### From Past Due

| Transition           | Probability |
| -------------------- | ----------- |
| Past Due → Active    | 55%         |
| Past Due → Past Due  | 10%         |
| Past Due → Canceled  | 35%         |

#### From Canceled

Canceled customers can only **reactivate to Active** — they cannot transition to Past Due.

| Transition           | Probability |
| -------------------- | ----------- |
| Canceled → Canceled  | 98%         |
| Canceled → Active    | 2%          |

#### New customer acquisition

Flat **7 new customers per month** on average, regardless of current base size.

- Per day: **3 independent Bernoulli rolls at ~7.8% each** (`(7 / 30) / 3 ≈ 0.0778`).
- This allows **0–3 new customers per day**, with an expected value of `3 × 0.0778 = 0.233` ≈ 7/month.
- More variance than a single 23% roll, but still trivial to implement.

### Sanity check vs. 30% YoY growth

- **Gross monthly churn** ≈ 3% (voluntary, Active → Canceled) + 3% × 35% (involuntary, Active → Past Due → Canceled) ≈ **~4%** of the active base.
- **Acquisition** is flat at 7/month rather than proportional, so growth is roughly linear: starting from ~40 active, we gain ~7/month and lose ~2/month → net ~+5/month, ending around 65–70 active after 6 months.
- This is **somewhat above** a strict 30% YoY pace but lands in the right ballpark for a growth-stage company; the trade-off is a much simpler, easier-to-reason-about acquisition model.

### Initial state distribution (open question)

Starting 50 customers — proposed split (to be confirmed):

| State     | Count |
| --------- | ----- |
| Active    | 40    |
| Past Due  | 5     |
| Canceled  | 5     |

## Technical Strategy

### SDK choice: `stripe-python`

We'll drive Stripe entirely through the official **`stripe-python`** SDK (v15.x). Considered alternatives — Stripe CLI fixtures and raw REST via `requests`/`httpx` — were rejected:

- **Fixtures** are declarative JSON with no loops, conditionals, or polling, so they can't express 50 customers × 180 days of stochastic state transitions or the async test-clock advance flow.
- **Raw REST** would force us to re-implement idempotency keys, retries, backoff, and error taxonomy — all of which `stripe-python` already provides.

`stripe-python` is first-party, actively maintained, covers every object we need, and lets the simulation be ordinary Python.

### SDK surface we'll use

| Object / Helper                          | Purpose                                                  |
| ---------------------------------------- | -------------------------------------------------------- |
| `stripe.Product` / `stripe.Price`        | One-time bootstrap of the 5 plan tiers + 3 paid add-ons. |
| `stripe.Customer`                        | Created with `test_clock=...` so each customer is bound to a clock. |
| `stripe.PaymentMethod` / `attach`        | Attach a successful or failing test card per customer.   |
| `stripe.Subscription` / `SubscriptionItem` | Create the subscription with plan + add-on items; cancel via `Subscription.cancel` or `cancel_at_period_end=True`. |
| `stripe.test_helpers.TestClock`          | `create`, `advance`, retrieve (poll until `ready`), and `delete` for cleanup. |
| `stripe.Invoice`                         | Read-only — invoices are produced as a side effect of clock advances and are what the BigQuery pipeline (Step 2) extracts. |

### Operational details the SDK gives us for free

- **Idempotency**: every POST will use a deterministic `idempotency_key` (e.g. `seed:v1:customer:{i}`) so reruns are no-ops. Stripe caches responses for ≥24h.
- **Rate limits**: test mode is **25 ops/sec global**. The SDK handles 429s with exponential backoff via `max_network_retries`.
- **Retries**: configured once at the SDK level rather than per-call.

### Stripe-imposed constraints we have to design around

These are facts about Stripe's API, not architectural choices — they shape the seeder regardless of how we structure it:

- **Test clocks hold ≤ 3 customers each**, so 50 customers requires **~17 clocks**.
- A customer's `test_clock` can only be set **at creation time** — it can't be retrofitted.
- `TestClock.advance` is **async** and only moves forward by at most **2 of the shortest billing interval per call** (so monthly subs cap at ~2 months per advance). We poll the clock until status flips from `advancing` to `ready`.
- Test clocks **auto-delete after 30 days** and cascade-delete their customers, which doubles as our cleanup mechanism.

### Driving the three subscription states

- **Active** — default test card `pm_card_visa`; invoices succeed on each clock advance.
- **Past Due** — attach the shared test PM `pm_card_chargeCustomerFail`. It attaches successfully but fails on collection, so the next renewal flips the subscription to `past_due`. (Stripe blocks raw card numbers via the API by default — use the documented test PM tokens.)
- **Canceled** — `Subscription.cancel(sub_id)` (immediate) or `Subscription.modify(sub_id, cancel_at_period_end=True)` (graceful).
