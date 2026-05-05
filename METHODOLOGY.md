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
Stripe's Billing dashboard doesn't display test-clock data, so a direct comparison to their MRR view isn't possible for this dataset. Stripe support confirmed: *"The Billing overview dashboard and its analytics don't include data from test clock subscriptions."* Their recommended workaround: *"Use the API to query your test clock subscriptions directly... and calculate metrics programmatically."*

That's exactly what this pipeline does — extract via the Stripe API into 
BigQuery, compute MRR via SQL, validate with an independent Python implementation. 
In production with real-time billing data, the dashboard would work and could 
serve as an additional reference.

## Validation approach
I took two approaches to validation

**Sanity Check Validation** 
Using the configuration values from the seeder, we calculate an expected subscription cost and subscriber numbers per month. Then we expect the estimated month revenue to be within 20% of the actual ones. This is a magnitude check, not a precision check, and was used early in the process to catch any catastropic regressions.

**Per-Customer Reconstruction**
Collects every customers paid invoices directly from Stripe using Python with the same period-spreading methodology as the SQL. Compares to the BigQuery output within $0.01. Agreement validates the ETL and SQL pipeline. 

**What this Doesn't Validate**
The methodology itself, both implementations use the same period-spreading logic. They could both be correct or wrong. Closing this gap would require verifying specific customers across plan tiers. A quick look over was completed, but a rigourous programmatic solution was not implemented withing the 2-day window.

## Known limitations
**Quantity=1 per customer.** All seed customers have a single screen, producing 5-10x smaller MRR magnitudes. The methodology correctly multiplies `unit_amount × quantity`; the seed just doesn't support it. However, the seed does support subscription changes (tier upgrades / downgrades), so we have confidence that the MRR correctly handles subscription modifications.

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