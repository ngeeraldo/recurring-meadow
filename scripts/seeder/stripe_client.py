import os

import stripe
from dotenv import load_dotenv

_MAX_NETWORK_RETRIES = 3


def init() -> None:
    load_dotenv()
    api_key = os.environ.get("STRIPE_API_KEY")
    if not api_key:
        raise RuntimeError("STRIPE_API_KEY is not set in the environment")
    stripe.api_key = api_key
    stripe.max_network_retries = _MAX_NETWORK_RETRIES
