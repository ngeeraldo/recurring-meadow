import time

import stripe

_POLL_INTERVAL_SECONDS = 1.0
_POLL_TIMEOUT_SECONDS = 120.0


def create_clock(frozen_time: int) -> stripe.test_helpers.TestClock:
    return stripe.test_helpers.TestClock.create(frozen_time=frozen_time)


def advance_clock(clock_id: str, frozen_time: int) -> stripe.test_helpers.TestClock:
    stripe.test_helpers.TestClock.advance(clock_id, frozen_time=frozen_time)

    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while True:
        clock = stripe.test_helpers.TestClock.retrieve(clock_id)
        if clock.status == "ready":
            return clock
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Test clock {clock_id} did not become ready within {_POLL_TIMEOUT_SECONDS}s")
        time.sleep(_POLL_INTERVAL_SECONDS)


def delete_clock(clock_id: str) -> None:
    stripe.test_helpers.TestClock.delete(clock_id)
