"""Circuit breaker unit suite — all ports faked in memory.

The breaker is the module that must work when everything else is on fire, so
the suite leans on failure paths: broker down, corrupt state file, latched
halts, and the cancel-before-close ordering that brokers actually enforce.
"""
from datetime import date, timedelta

import pytest

from tradeops.safety import CircuitBreaker

TEST_THRESHOLDS = {"l1_daily_loss_pct": 2.0, "l2_drawdown_pct": 10.0, "l3_drawdown_pct": 15.0}


def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


class FakeBroker:
    def __init__(self, portfolio=None, positions=None, orders=None):
        self.portfolio = portfolio
        self.positions = positions or []
        self.orders = orders or []
        self.fail_portfolio = False
        self.calls = []  # ordered log of state-changing calls

    def get_portfolio(self):
        if self.fail_portfolio:
            raise ConnectionError("broker unreachable")
        return self.portfolio

    def get_positions(self):
        return self.positions

    def get_open_orders(self):
        return self.orders

    def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))

    def close_position(self, ticker):
        self.calls.append(("close", ticker))

    def wait_order_terminal(self, order_id):
        self.calls.append(("wait", order_id))


class FakeJournal:
    def __init__(self, history=None, hwm=None):
        self.history = history or []  # newest first, like the real journal
        self.hwm = hwm
        self.snapshots = []
        self.events = []
        self.trades = []

    def log_equity_snapshot(self, equity, cash, buying_power, source):
        self.snapshots.append({"equity": equity, "source": source})

    def get_equity_history(self, days):
        return self.history

    def get_high_water_mark(self):
        return self.hwm

    def log_breaker_event(self, event, old_level, new_level, equity=None,
                          daily_loss_pct=None, drawdown_pct=None, hwm=None,
                          reason="", operator=None):
        self.events.append({"event": event, "old": old_level, "new": new_level,
                            "reason": reason})

    def log_trade(self, ticker, side, **fields):
        self.trades.append({"ticker": ticker, "side": side, **fields})


class FakeNotifier:
    def __init__(self):
        self.alerts = []

    def send_alert(self, message, severity="INFO"):
        self.alerts.append((severity, message))


@pytest.fixture
def make_breaker(tmp_path):
    def _make(equity=100_000.0, history=None, hwm=None, positions=None, orders=None):
        broker = FakeBroker(
            portfolio={"total_value": equity, "cash": equity, "buying_power": equity},
            positions=positions, orders=orders)
        journal = FakeJournal(history=history, hwm=hwm)
        notifier = FakeNotifier()
        cb = CircuitBreaker(broker, journal, notifier,
                            state_path=str(tmp_path / "halt_state.json"),
                            thresholds=TEST_THRESHOLDS)
        return cb, broker, journal, notifier
    return _make


# ── evaluation ─────────────────────────────────────────────────────────────────


def test_healthy_account_is_level_0(make_breaker):
    cb, *_ = make_breaker(
        equity=100_000, hwm=100_000,
        history=[{"date": days_ago(1), "equity": 100_000}])
    ev = cb.check_and_update()
    assert ev["level"] == 0
    assert cb.entries_allowed() == (True, "no active halt")


def test_daily_loss_trips_soft_halt(make_breaker):
    cb, *_ = make_breaker(
        equity=97_000, hwm=100_000,
        history=[{"date": days_ago(1), "equity": 100_000}])
    ev = cb.check_and_update()
    assert ev["level"] == 1
    assert "daily loss" in ev["reason"]
    allowed, why = cb.entries_allowed()
    assert not allowed and "soft halt" in why


def test_material_losing_streak_trips_soft_halt(make_breaker):
    cb, *_ = make_breaker(
        equity=96_000, hwm=99_000,
        history=[{"date": days_ago(1), "equity": 97_000},
                 {"date": days_ago(2), "equity": 98_000},
                 {"date": days_ago(3), "equity": 99_000}])
    ev = cb.evaluate(96_000)
    assert ev["losing_days"] == 3
    assert ev["level"] == 1
    assert "consecutive losing days" in ev["reason"]


def test_immaterial_drift_does_not_count_as_streak(make_breaker):
    # three "down" days of ~0.03% each — a magnitude-blind rule would halt here
    cb, *_ = make_breaker(
        equity=99_910, hwm=100_000,
        history=[{"date": days_ago(1), "equity": 99_940},
                 {"date": days_ago(2), "equity": 99_970},
                 {"date": days_ago(3), "equity": 100_000}])
    ev = cb.evaluate(99_910)
    assert ev["losing_days"] == 0
    assert ev["level"] == 0


def test_drawdown_trips_hard_halt(make_breaker):
    cb, *_ = make_breaker(equity=89_000, hwm=100_000)
    ev = cb.check_and_update()
    assert ev["level"] == 2
    assert ev["requires_manual_reset"]
    allowed, why = cb.entries_allowed()
    assert not allowed and "manual reset required" in why


def test_hard_halt_latches_through_recovery(make_breaker):
    cb, broker, *_ = make_breaker(equity=89_000, hwm=100_000)
    assert cb.check_and_update()["level"] == 2
    # equity recovers fully — the latch must hold until a human resets it
    broker.portfolio = {"total_value": 100_000, "cash": 0, "buying_power": 0}
    ev = cb.check_and_update()
    assert ev["level"] == 2
    assert not cb.entries_allowed()[0]


def test_flatten_cancels_all_orders_before_closing(make_breaker):
    cb, broker, journal, notifier = make_breaker(
        equity=84_000, hwm=100_000,
        positions=[{"ticker": "AAA", "shares": 10, "avg_cost": 50.0, "current_price": 45.0}],
        orders=[{"id": "o1", "ticker": "AAA"}, {"id": "o2", "ticker": "BBB"}])
    ev = cb.check_and_update()
    assert ev["level"] == 3
    # every cancel (and its terminal-state wait) must precede the first close
    kinds = [k for k, _ in broker.calls]
    assert kinds.index("close") > max(i for i, k in enumerate(kinds) if k in ("cancel", "wait"))
    assert kinds.count("cancel") == 2
    # the liquidation is journaled with its P&L
    assert journal.trades[0]["side"] == "SELL"
    assert journal.trades[0]["pnl_pct"] == -10.0
    assert any(sev == "CRITICAL" and "FLATTEN" in msg for sev, msg in notifier.alerts)


def test_flatten_records_failures_without_raising(make_breaker):
    cb, broker, *_ = make_breaker(equity=84_000, hwm=100_000)
    broker.get_open_orders = lambda: (_ for _ in ()).throw(ConnectionError("api down"))
    results = cb.flatten_all()
    assert any("order fetch" in f for f in results["failed"])


# ── fail-closed behavior ───────────────────────────────────────────────────────


def test_broker_failure_fails_closed_for_entries(make_breaker):
    cb, broker, journal, _ = make_breaker()
    broker.fail_portfolio = True
    ev = cb.check_and_update()
    assert ev["fail_closed"]
    allowed, why = cb.entries_allowed()
    assert not allowed and "fail-closed" in why
    assert any(e["event"] == "FAIL_CLOSED" for e in journal.events)


def test_corrupt_state_file_fails_closed(make_breaker, tmp_path):
    cb, *_ = make_breaker()
    (tmp_path / "halt_state.json").write_text("{not json")
    allowed, why = cb.entries_allowed()
    assert not allowed and "fail closed" in why


# ── halt lifecycle ─────────────────────────────────────────────────────────────


def test_soft_halt_expires_next_day(make_breaker, tmp_path):
    cb, *_ = make_breaker()
    state = cb._default_state()
    state.update(level=1, reason="daily loss", trigger_date=days_ago(1))
    cb._write_halt_state(state)
    allowed, _ = cb.entries_allowed()
    assert allowed  # yesterday's soft halt no longer blocks


def test_manual_reset_clears_hard_halt(make_breaker):
    cb, _, journal, _ = make_breaker(equity=89_000, hwm=100_000)
    assert cb.check_and_update()["level"] == 2
    cb.manual_reset("post-incident reset after review")
    assert cb.entries_allowed()[0]
    assert any(e["event"] == "MANUAL_RESET" for e in journal.events)


def test_drill_blocks_entries_and_restores_state(make_breaker):
    cb, _, journal, _ = make_breaker(
        equity=100_000, hwm=100_000,
        history=[{"date": days_ago(1), "equity": 100_000}])
    cb.check_and_update()
    result = cb.run_drill()
    assert result["passed"]
    assert cb.entries_allowed()[0]  # pre-drill state restored
    assert any(e["event"] == "DRILL" for e in journal.events)
