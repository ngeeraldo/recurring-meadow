import pytest
import stripe

from scripts.seeder import stripe_client


def test_init_sets_api_key_from_env(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_abc123")
    stripe.api_key = None

    stripe_client.init()

    assert stripe.api_key == "sk_test_abc123"


def test_init_configures_network_retries(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_abc123")
    stripe.max_network_retries = 0

    stripe_client.init()

    assert stripe.max_network_retries >= 2


def test_init_raises_when_api_key_missing(monkeypatch):
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    monkeypatch.setattr("scripts.seeder.stripe_client.load_dotenv", lambda: None)

    with pytest.raises(RuntimeError, match="STRIPE_API_KEY"):
        stripe_client.init()
