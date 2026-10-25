from datetime import UTC, datetime, timedelta

from app.monitoring import (
    ALERT_AFTER_FAILED_CYCLES,
    STALE_AFTER,
    check_heartbeat,
    record_cycle,
    record_last_error,
)
from tests.factories import FakeAlerter, FakeRedis

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


async def test_alert_once_after_failed_cycles_and_on_recovery() -> None:
    redis, alerter = FakeRedis(), FakeAlerter()
    await record_last_error(redis, "HHAPIError(403 forbidden)")

    for _ in range(ALERT_AFTER_FAILED_CYCLES + 3):
        await record_cycle(redis, alerter, checked=0, not_checked=2, now=NOW)

    assert len(alerter.alerts) == 1
    assert "403 forbidden" in alerter.alerts[0]

    await record_cycle(redis, alerter, checked=2, not_checked=0, now=NOW)
    await record_cycle(redis, alerter, checked=2, not_checked=0, now=NOW)
    assert alerter.alerts[1:] == ["✅ Проверка подписок снова работает."]


async def test_partial_failures_and_empty_cycles_do_not_alert() -> None:
    redis, alerter = FakeRedis(), FakeAlerter()

    for _ in range(ALERT_AFTER_FAILED_CYCLES * 2):
        await record_cycle(redis, alerter, checked=1, not_checked=1, now=NOW)
        await record_cycle(redis, alerter, checked=0, not_checked=0, now=NOW)

    assert alerter.alerts == []


async def test_heartbeat_stale_alert_and_recovery() -> None:
    redis, alerter = FakeRedis(), FakeAlerter()
    await record_cycle(redis, alerter, checked=1, not_checked=0, now=NOW)

    await check_heartbeat(redis, alerter, NOW + timedelta(minutes=1))
    assert alerter.alerts == []

    late = NOW + STALE_AFTER + timedelta(seconds=1)
    await check_heartbeat(redis, alerter, late)
    await check_heartbeat(redis, alerter, late + timedelta(minutes=1))
    assert len(alerter.alerts) == 1
    assert alerter.alerts[0].startswith("🛑")

    await record_cycle(redis, alerter, checked=1, not_checked=0, now=late)
    await check_heartbeat(redis, alerter, late)
    assert alerter.alerts[1] == "✅ Воркер снова работает."


async def test_missing_heartbeat_is_stale() -> None:
    redis, alerter = FakeRedis(), FakeAlerter()

    await check_heartbeat(redis, alerter, NOW)

    assert len(alerter.alerts) == 1
