import stripe

# Pricing per refs/pricing.md, in USD cents.
# "annual" column there is the effective monthly rate when paying annually,
# so the yearly Stripe Price is that rate * 12.
PLANS = {
    "free":       {"name": "Free",       "monthly_cents":    0, "annual_effective_cents":    0},
    "standard":   {"name": "Standard",   "monthly_cents": 1000, "annual_effective_cents":  900},
    "pro_plus":   {"name": "Pro Plus",   "monthly_cents": 1500, "annual_effective_cents": 1350},
    "engage":     {"name": "Engage",     "monthly_cents": 3000, "annual_effective_cents": 2700},
    "enterprise": {"name": "Enterprise", "monthly_cents": 4500, "annual_effective_cents": 4050},
}


def get_or_create_product(plan_slug: str) -> stripe.Product:
    plan = PLANS[plan_slug]
    product_id = f"plan_{plan_slug}"
    try:
        return stripe.Product.retrieve(product_id)
    except stripe.error.InvalidRequestError:
        return stripe.Product.create(id=product_id, name=plan["name"])


def get_or_create_price(plan_slug: str, interval: str) -> stripe.Price:
    if interval not in ("month", "year"):
        raise ValueError(f"interval must be 'month' or 'year', got {interval!r}")

    suffix = "monthly" if interval == "month" else "yearly"
    lookup_key = f"{plan_slug}_{suffix}"

    existing = stripe.Price.list(lookup_keys=[lookup_key], limit=1)
    if existing.data:
        return existing.data[0]

    plan = PLANS[plan_slug]
    if interval == "month":
        unit_amount = plan["monthly_cents"]
    else:
        unit_amount = plan["annual_effective_cents"] * 12

    product = get_or_create_product(plan_slug)
    return stripe.Price.create(
        lookup_key=lookup_key,
        product=product.id,
        currency="usd",
        unit_amount=unit_amount,
        recurring={"interval": interval},
    )
