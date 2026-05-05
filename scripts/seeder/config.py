"""Tunable knobs for the seeder simulation.

Per-day transition probabilities are derived from the monthly figures in
refs/seeder.md by simple division by 30. This is approximate (the exact
conversion is the matrix root M^(1/30)) but well within noise for a
180-day run with 10-16 customers.
"""

# --- Simulation scale -----------------------------------------------------
# 3.5 new/month combined with the ~4% effective monthly churn below targets
# roughly 30% year-over-year growth on a 50-customer base.
INITIAL_CUSTOMER_COUNT = 50
SIMULATION_DAYS = 180
NEW_CUSTOMERS_PER_MONTH_AVG = 3.5

# --- Determinism ----------------------------------------------------------
RNG_SEED = 42

# --- Tier ladder & weights ------------------------------------------------
# Ordered low → high. Standard is the floor (no downgrade past it);
# Enterprise is the ceiling (no upgrade past it).
TIER_LADDER = ["standard", "pro_plus", "engage", "enterprise"]

INITIAL_TIER_WEIGHTS = {
    "standard":   0.60,
    "pro_plus":   0.25,
    "engage":     0.10,
    "enterprise": 0.05,
}

INITIAL_CADENCE_WEIGHTS = {
    "month": 0.80,
    "year":  0.20,
}

# --- Daily transition probabilities ---------------------------------------
# Outgoing probabilities for each "from" state. The remainder (1 - sum) is
# "stay in current state".
FROM_ACTIVE = {
    "past_due":  0.03 / 30,  # 0.100%
    "canceled":  0.03 / 30,  # 0.100%
    "upgrade":   0.02 / 30,  # 0.067%
    "downgrade": 0.01 / 30,  # 0.033%
}

FROM_PAST_DUE = {
    "active":   0.55 / 30,   # 1.833%
    "canceled": 0.35 / 30,   # 1.167%
}

FROM_CANCELED = {
    "active":   0.02 / 30,   # 0.067%
}

# --- New-customer acquisition ---------------------------------------------
# Three Bernoulli rolls per simulated day. Expected value =
# rolls * p = NEW_CUSTOMERS_PER_MONTH_AVG / 30.
ACQUISITION_ROLLS_PER_DAY = 3
ACQUISITION_P_PER_ROLL = (
    NEW_CUSTOMERS_PER_MONTH_AVG / 30 / ACQUISITION_ROLLS_PER_DAY
)
