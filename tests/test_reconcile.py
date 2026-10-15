"""Every state of the reconciler, with a hand-driven clock. Ported from the Kiosk plugin's ClockRulesTest."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.second_hand import protocol
from custom_components.second_hand.reconcile import ClockTimer, Command, Reconciler

NY = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 14, 12, 0, 0, tzinfo=NY)
A, B, C = "assist:01JAAAAAAAAAAAAAAAAAAAAAAA", "assist:01JBBBBBBBBBBBBBBBBBBBBBBB", "helper:timer.tea"


def s(n: float) -> timedelta:
    return timedelta(seconds=n)


def timers(*items: ClockTimer) -> dict[str, ClockTimer]:
    return {t.key: t for t in items}


def running(key: str, ends_at: datetime) -> ClockTimer:
    return ClockTimer(key, ends_at)


def paused(key: str) -> ClockTimer:
    return ClockTimer(key, None)


def labels(cmds: list[Command]) -> list[str]:
    return [c.label for c in cmds]


def connected(now: datetime = T0, snapshot: dict[str, ClockTimer] | None = None) -> Reconciler:
    r = Reconciler(NY)
    r.on_connected(now, snapshot or {})
    return r


def test_connect_sends_time_and_clear() -> None:
    cmds = Reconciler(NY).on_connected(T0, {})
    assert labels(cmds) == ["timeinfo", "ClearIndicator"]
    assert cmds[0].payload == protocol.time_info("2026-10-14T12:00:00-04:00")
    assert cmds[1].payload == protocol.clear_indicator()


def test_new_timer_is_shown_with_its_end_time() -> None:
    r = connected()
    cmds = r.update(T0 + s(1), timers(running(A, T0 + s(91))))
    assert labels(cmds) == ["SetAlert TIMER"]
    assert cmds[0].payload == protocol.set_timer(protocol.alert_token(A), "2026-10-14T12:01:31-04:00")
    assert r.shown_count == 1


def test_same_snapshot_sends_nothing_and_jitter_is_ignored() -> None:
    r = connected()
    r.update(T0, timers(running(A, T0 + s(90))))
    assert r.update(T0 + s(5), timers(running(A, T0 + s(90)))) == []
    assert r.update(T0 + s(6), timers(running(A, T0 + s(91)))) == []  # whole-second rounding in HA


def test_cancel_deletes_it_once() -> None:
    r = connected()
    r.update(T0, timers(running(A, T0 + s(120))))
    cmds = r.update(T0 + s(5), {})
    assert labels(cmds) == ["DeleteAlert"]
    assert cmds[0].payload == protocol.delete_alert(protocol.alert_token(A))
    assert r.update(T0 + s(6), {}) == []


def test_timer_that_ends_by_itself_is_not_deleted() -> None:
    r = connected()
    r.update(T0, timers(running(A, T0 + s(30))))
    assert r.update(T0 + s(29.5), {}) == []  # HA dropped it at its end: the clock clears itself


def test_finish_pulses_once_about_a_second_later() -> None:
    r = connected()
    end = T0 + s(90)
    r.update(T0, timers(running(A, end)))
    assert r.on_finished(end, A) == []
    assert r.update(end, {}) == []
    assert r.next_due() == end + s(1)
    assert r.tick(end + s(0.5)) == []
    cmds = r.tick(end + s(1))
    assert labels(cmds) == ["timers=active"]
    assert cmds[0].payload == protocol.timers_active()
    assert r.tick(end + s(12)) == []  # never repeated, like a real Echo
    assert r.next_due() > end + s(12)


def test_two_timers_finishing_together_pulse_once() -> None:
    r = connected()
    end = T0 + s(60)
    r.update(T0, timers(running(A, end), running(C, end)))
    r.on_finished(end, A)
    r.on_finished(end, C)
    assert labels(r.tick(end + s(1))) == ["timers=active"]


def test_finished_timer_still_in_snapshot_is_not_reshown() -> None:
    r = connected()
    end = T0 + s(10)
    r.update(T0, timers(running(A, end)))
    r.on_finished(end - s(0.3), A)  # the source may report the finish a moment early
    assert r.update(end - s(0.2), timers(running(A, end))) == []


def test_pause_deletes_and_resume_reuses_token() -> None:
    r = connected()
    r.update(T0, timers(running(A, T0 + s(600))))
    assert labels(r.update(T0 + s(60), timers(paused(A)))) == ["DeleteAlert"]
    assert r.shown_count == 0
    cmds = r.update(T0 + s(120), timers(running(A, T0 + s(660))))
    assert labels(cmds) == ["SetAlert TIMER"]
    assert cmds[0].payload == protocol.set_timer(protocol.alert_token(A), "2026-10-14T12:11:00-04:00")


def test_time_added_changes_end() -> None:
    r = connected()
    r.update(T0, timers(running(A, T0 + s(300))))
    cmds = r.update(T0 + s(10), timers(running(A, T0 + s(430))))
    assert labels(cmds) == ["SetAlert TIMER"]
    assert cmds[0].payload == protocol.set_timer(protocol.alert_token(A), "2026-10-14T12:07:10-04:00")


def test_several_timers_in_snapshot_order() -> None:
    r = connected()
    cmds = r.update(T0, timers(running(A, T0 + s(90)), running(B, T0 + s(120)), paused(C)))
    assert labels(cmds) == ["SetAlert TIMER", "SetAlert TIMER"]
    assert labels(r.update(T0 + s(5), timers(running(A, T0 + s(90)), paused(C)))) == ["DeleteAlert"]


def test_nothing_while_disconnected() -> None:
    r = Reconciler(NY)
    assert r.update(T0, timers(running(A, T0 + s(90)))) == []
    assert r.on_finished(T0, A) == []
    assert r.tick(T0 + s(3600)) == []
    assert r.next_due() is None
    assert r.resync_time(T0) == []


def test_reconnect_resends_only_what_is_still_running() -> None:
    r = connected()
    r.update(T0, timers(running(A, T0 + s(600)), running(B, T0 + s(30))))
    r.on_disconnected()
    snapshot = timers(running(A, T0 + s(600)), running(B, T0 + s(30)), running(C, T0 + s(910)))
    cmds = r.on_connected(T0 + s(60), snapshot)  # B has ended by now; C started while the link was down
    assert labels(cmds) == ["timeinfo", "ClearIndicator", "SetAlert TIMER", "SetAlert TIMER"]
    assert cmds[2].payload == protocol.set_timer(protocol.alert_token(A), "2026-10-14T12:10:00-04:00")
    assert cmds[3].payload == protocol.set_timer(protocol.alert_token(C), "2026-10-14T12:15:10-04:00")


def test_missed_finish_is_not_replayed() -> None:
    r = connected()
    r.update(T0, timers(running(A, T0 + s(30))))
    r.on_disconnected()
    r.on_finished(T0 + s(30), A)
    assert labels(r.on_connected(T0 + s(120), {})) == ["timeinfo", "ClearIndicator"]
    assert r.tick(T0 + s(130)) == []


def test_pulse_pending_at_disconnect_is_dropped() -> None:
    r = connected()
    r.on_finished(T0, A)
    r.on_disconnected()
    r.on_connected(T0 + s(0.5), {})
    assert r.tick(T0 + s(2)) == []


def test_half_hour_time_info() -> None:
    r = connected(T0 + timedelta(minutes=7))
    half = T0 + timedelta(minutes=30)
    assert r.next_due() == half
    assert r.tick(half - s(0.001)) == []
    cmds = r.tick(half)
    assert labels(cmds) == ["timeinfo"]
    assert cmds[0].payload == protocol.time_info("2026-10-14T12:30:00-04:00")
    assert r.next_due() == T0 + timedelta(hours=1)
    # A late tick sends once and moves to the next boundary.
    assert labels(r.tick(T0 + timedelta(hours=3, seconds=5))) == ["timeinfo"]
    assert r.next_due() == T0 + timedelta(hours=3, minutes=30)


def test_half_hour_across_dst_end() -> None:
    # 2026-11-01: 01:59:59 EDT (05:59:59 UTC) is followed by 01:00:00 EST.
    before = datetime(2026, 11, 1, 5, 45, tzinfo=UTC)  # 01:45 EDT
    r = connected(before)
    due = r.next_due()
    assert due == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)  # 15 min later, not 75
    cmds = r.tick(due)
    assert cmds[0].payload == protocol.time_info("2026-11-01T01:00:00-05:00")  # the clock goes back an hour
    assert r.next_due() == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


def test_half_hour_across_dst_start() -> None:
    # 2026-03-08: 01:59:59 EST (06:59:59 UTC) is followed by 03:00:00 EDT.
    r = connected(datetime(2026, 3, 8, 6, 45, tzinfo=UTC))  # 01:45 EST
    assert r.next_due() == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)
    assert r.tick(datetime(2026, 3, 8, 7, 0, tzinfo=UTC))[0].payload == protocol.time_info("2026-03-08T03:00:00-04:00")


def test_half_hour_in_a_half_hour_offset_zone() -> None:
    r = Reconciler(ZoneInfo("Asia/Kolkata"))  # UTC+05:30
    r.on_connected(datetime(2026, 10, 14, 6, 40, tzinfo=UTC), {})  # 12:10 local
    assert r.next_due() == datetime(2026, 10, 14, 7, 0, tzinfo=UTC)  # 12:30 local


def test_manual_resync() -> None:
    r = connected()
    assert labels(r.resync_time(T0 + s(5))) == ["timeinfo"]


def test_finish_for_an_unseen_timer_still_pulses() -> None:
    r = connected()
    r.on_finished(T0, "assist:late")
    assert labels(r.tick(T0 + s(1))) == ["timers=active"]
