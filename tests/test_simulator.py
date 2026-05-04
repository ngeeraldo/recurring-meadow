from scripts import config, simulator


def test_simulate_is_deterministic_with_seed():
    roster1, events1 = simulator.simulate(seed=42)
    roster2, events2 = simulator.simulate(seed=42)

    assert [c.sim_id for c in roster1] == [c.sim_id for c in roster2]
    assert [(e.day, e.sim_id, e.type, e.payload) for e in events1] == \
           [(e.day, e.sim_id, e.type, e.payload) for e in events2]


def test_simulate_initial_roster_size_matches_config():
    roster, _ = simulator.simulate(seed=42)
    initial = [c for c in roster if c.created_day == 0]
    assert len(initial) == config.INITIAL_CUSTOMER_COUNT


def test_simulate_emits_one_creation_event_per_customer():
    roster, events = simulator.simulate(seed=42)
    creates = [e for e in events if e.type == "customer_created"]
    assert len(creates) == len(roster)
    assert {e.sim_id for e in creates} == {c.sim_id for c in roster}


def test_initial_customers_use_valid_tier_and_cadence():
    roster, _ = simulator.simulate(seed=42)
    for c in roster:
        if c.created_day == 0:
            assert c.tier in config.TIER_LADDER
            assert c.cadence in config.INITIAL_CADENCE_WEIGHTS


def test_standard_customer_never_emits_downgrade(monkeypatch):
    """Boundary protection: Standard is the floor, downgrade rolls stay put."""
    monkeypatch.setattr(config, "INITIAL_CUSTOMER_COUNT", 1)
    monkeypatch.setattr(config, "INITIAL_TIER_WEIGHTS", {"standard": 1.0})
    monkeypatch.setattr(config, "FROM_ACTIVE", {
        "past_due": 0.0, "canceled": 0.0, "upgrade": 0.0, "downgrade": 1.0,
    })
    monkeypatch.setattr(config, "SIMULATION_DAYS", 30)
    monkeypatch.setattr(config, "ACQUISITION_P_PER_ROLL", 0.0)

    roster, events = simulator.simulate(seed=42)
    downgrades = [e for e in events if e.type == "tier_downgraded"]

    assert downgrades == []
    assert roster[0].tier == "standard"


def test_enterprise_customer_never_emits_upgrade(monkeypatch):
    """Boundary protection: Enterprise is the ceiling, upgrade rolls stay put."""
    monkeypatch.setattr(config, "INITIAL_CUSTOMER_COUNT", 1)
    monkeypatch.setattr(config, "INITIAL_TIER_WEIGHTS", {"enterprise": 1.0})
    monkeypatch.setattr(config, "FROM_ACTIVE", {
        "past_due": 0.0, "canceled": 0.0, "upgrade": 1.0, "downgrade": 0.0,
    })
    monkeypatch.setattr(config, "SIMULATION_DAYS", 30)
    monkeypatch.setattr(config, "ACQUISITION_P_PER_ROLL", 0.0)

    roster, events = simulator.simulate(seed=42)
    upgrades = [e for e in events if e.type == "tier_upgraded"]

    assert upgrades == []
    assert roster[0].tier == "enterprise"


def test_tier_change_payloads_carry_from_and_to():
    """When tier changes do fire, payload should include from/to."""
    roster, events = simulator.simulate(seed=42)
    for e in events:
        if e.type in ("tier_upgraded", "tier_downgraded"):
            assert "from" in e.payload
            assert "to" in e.payload
            assert e.payload["from"] in config.TIER_LADDER
            assert e.payload["to"] in config.TIER_LADDER
