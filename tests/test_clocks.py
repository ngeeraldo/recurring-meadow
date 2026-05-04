from unittest.mock import MagicMock, patch

from scripts import clocks


def test_create_clock_passes_frozen_time():
    created = MagicMock(id="clock_abc")
    with patch("stripe.test_helpers.TestClock.create", return_value=created) as create:
        result = clocks.create_clock(frozen_time=1_700_000_000)

    create.assert_called_once_with(frozen_time=1_700_000_000)
    assert result is created


def test_advance_clock_polls_until_ready():
    advancing = MagicMock(status="advancing")
    ready = MagicMock(status="ready")
    advance_response = MagicMock(status="advancing")

    with patch("stripe.test_helpers.TestClock.advance", return_value=advance_response) as advance, \
         patch("stripe.test_helpers.TestClock.retrieve", side_effect=[advancing, advancing, ready]) as retrieve, \
         patch("scripts.clocks.time.sleep") as sleep:
        result = clocks.advance_clock("clock_abc", frozen_time=1_700_000_000 + 86_400)

    advance.assert_called_once_with("clock_abc", frozen_time=1_700_000_000 + 86_400)
    assert retrieve.call_count == 3
    assert sleep.call_count >= 2
    assert result is ready


def test_delete_clock_calls_sdk_once():
    with patch("stripe.test_helpers.TestClock.delete", return_value=MagicMock()) as delete:
        clocks.delete_clock("clock_abc")

    delete.assert_called_once_with("clock_abc")
