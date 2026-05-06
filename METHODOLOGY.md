# MRR Methodology

## What MRR is
Monthly Recurring Revenue (MRR) is the normalized monthly value of active recurring subscriptions.
It is mainly used as a prediction tool of future earnings.

## Source data
Three approaches were considered: 

**Subscription State** 
Query current subscriptions, compute MRR from plans occurring during targeted months. Simple, but breaks when user changes subscription type (loses historical context)

**Event-based Reconstruction** 
Track subscription events, maintaining a running MRR tally. This is what production analytics platforms use, it correclty handles historical state and subscription movement. Out of scope for a 2-day build

**Invoice line items (chosen)** 
Every paid line item represents a specific dollar amount billed. Invoices are immutable and easily auditable. This was the right size for the project: simpler than event sourcing, more accurate than subscription state.

## Filters and normalization 
Two decisions narrow what counts as MRR:

**Active-only.** 
Past_due subscriptions are excluded. Stripe's default analytics include them, but conservative interpretations are stronger for financial metrics. 

**Annual normalized to monthly.** 
An annual subscription priced at $324/year contributes $27/month to MRR for each of the 12 months its billing period covers. This follows standard B2B SaaS convention (Stripe Billing, ChartMogul, etc. blend this way) and aligns with the spec's "normalized monthly value" language.

## Comparison to Stripe's analytics
Stripe's dashboard MRR view shows ~$2,800/month — roughly 3x my calculated MRR. The cause is data pollution from prior seeder runs: Stripe's dashboard reports 167 active subscribers, but my current seed contains only ~70 customers. Stripe's "delete all test data" function does not reliably remove all data, so I can not clearly compare the two MRRs. 

Subscriber count alone (~2.4x) accounts for most of the 3x MRR difference. Methodology differences likely contribute too — Stripe appears to compute from subscription state and includes past_due subscriptions; I use invoice line items and exclude past_due. Without scoping Stripe's view to my current customer set, I can't quantify which portion of the gap is which.

## Validation approach
I took two approaches to validation

**Sanity Check Validation** 
Using the configuration values from the seeder, we calculate an expected subscription cost and subscriber numbers per month. Then we expect the estimated month revenue to be within 20% of the actual ones. This is a magnitude check, not a precision check, and was used early in the process to catch any catastropic regressions.

**Per-Customer Reconstruction**
Collects every customers paid invoices directly from Stripe using Python with the same period-spreading methodology as the SQL. Compares to the BigQuery output within $0.01. Agreement validates the ETL and SQL pipeline. 

**Stage 3 — Per-Customer Spot Check.** 
Produces an audit-trail format: for each of ~70 seeded customers, the customer's event log (from `seeder_events.txt`) is printed alongside their per-month MRR contribution. This makes it possible to verify specific customer scenarios — vanilla monthly subs, mid-period tier changes (with proration), cancellations, past_due transitions — produce the expected MRR shapes. After this re-run, all 70 customers' MRR contributions match expectations given their event histories.

The full audit is committed as `output/validation_output.txt`.

**Limitations of this validation.** 
Stage 3 verification is human-in-the-loop — the tool surfaces customer-level data, but a person reads it. For long-running production data with thousands of customers, this approach doesn't scale. Production validation would encode invariant checks as automated assertions.

## Known limitations
**Quantity=1 per customer.** 
All seed customers have a single screen, producing 5-10x smaller MRR magnitudes. The methodology correctly multiplies `unit_amount × quantity`; the seed just doesn't support it. However, the seed does support subscription changes (tier upgrades / downgrades), so we have confidence that the MRR correctly handles subscription modifications.

## Architecture for production sync
Webhook-driven incremental sync (subscribe to invoice.paid, 
customer.subscription.updated, etc.) for freshness, plus a daily batch 
reconciliation job to catch missed events. Stripe Data Pipeline is a 
viable managed alternative.

## Production retrospective
The biggest upgrade would be event-based MRR with movement decomposition 
(new/expansion/contraction/churn). Period-spreading tells you that MRR 
changed; movement decomposition tells you why. Plus aggressive caching of 
the API layer and multi-currency normalization for international customers.