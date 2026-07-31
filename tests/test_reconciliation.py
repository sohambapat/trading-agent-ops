"""Reconciliation unit suite — broker and ledger faked in memory.

Two guarantees carry most of the weight: a lookup that fails leaves the row
pending (never resolved by assumption), and a position that left the broker
book without a journal row gets one — with the exit that cannot be explained
paging a human instead of being guessed at.
"""
import pytest

from tradeops.journal import DEFAULT_TOLERANCES, FillReconciler, expectancy_report

# ── fakes ──────────────────────────────────────────────────────────────────


class FakeBroker:
    def __init__(self, positions=None):
        self._positions = positions or []

    def get_positions(self):
        return list(self._positions)


class FakeOrders:
    """The post-trade lookup surface. `None` means the lookup failed."""

    def __init__(self, orders=None, sells=None, opens=None):
        self.orders = orders or {}
        self.sells = sells or {}
        self.opens = opens or {}

    def get_order(self, order_id):
        return self.orders.get(order_id)

    def get_recent_filled_sell(self, ticker, since_iso):
        self.last_since = since_iso
        return self.sells.get(ticker)

    def get_daily_open(self, ticker, day):
        return self.opens.get((ticker, day)) or self.opens.get(ticker)


class FakeLedger:
    def __init__(self, buys=None, sells=None, unclosed=None, decompose=None):
        self._buys = buys or []
        self._sells = sells or []
        self._unclosed = unclosed or []
        self._decompose = decompose or []
        self.buy_fills = []
        self.sell_fills = []
        self.failed_sells = []
        self.logged_trades = []
        self.logged_events = []
        self.decompositions = []

    def get_pending_buy_fills(self, days):
        return list(self._buys)

    def set_buy_fill(self, trade_id, filled_avg_price=None, slippage_pct=None,
                     fill_status="FILLED"):
        self.buy_fills.append((trade_id, filled_avg_price, slippage_pct, fill_status))

    def get_pending_sell_fills(self, days):
        return list(self._sells)

    def settle_sell_fill(self, trade_id, exit_price, pnl_pct, filled_avg_price=None):
        self.sell_fills.append((trade_id, exit_price, pnl_pct))

    def mark_sell_failed(self, trade_id):
        self.failed_sells.append(trade_id)

    def get_unclosed_entries(self):
        return list(self._unclosed)

    def get_fills_needing_decomposition(self, days):
        return list(self._decompose)

    def set_fill_decomposition(self, trade_id, gap_pct, exec_slippage_pct):
        self.decompositions.append((trade_id, gap_pct, exec_slippage_pct))

    def log_trade(self, ticker, side, **fields):
        self.logged_trades.append({"ticker": ticker, "side": side, **fields})

    def log_position_event(self, ticker, event_type, **fields):
        self.logged_events.append({"ticker": ticker, "event_type": event_type, **fields})


class FakeNotifier:
    def __init__(self):
        self.alerts = []

    def send_alert(self, message, severity="INFO"):
        self.alerts.append((severity, message))

    def severities(self):
        return [s for s, _ in self.alerts]


def make(broker=None, orders=None, ledger=None, **kw):
    notifier = FakeNotifier()
    ledger = ledger or FakeLedger()
    reconciler = FillReconciler(broker or FakeBroker(), orders or FakeOrders(),
                                ledger, notifier, **kw)
    return reconciler, ledger, notifier


# ── entry fills ────────────────────────────────────────────────────────────


def test_entry_fill_records_price_and_slippage():
    ledger = FakeLedger(buys=[{"id": 1, "ticker": "AAA", "order_id": "o1",
                               "entry_price": 100.0}])
    orders = FakeOrders(orders={"o1": {"status": "filled", "filled_avg_price": 100.2}})
    rec, ledger, notifier = make(orders=orders, ledger=ledger)

    result = rec.settle_entry_fills()
    assert result.settled == 1
    assert ledger.buy_fills == [(1, 100.2, 0.2, "FILLED")]
    assert notifier.alerts == []  # 0.2% is inside the warning threshold


def test_entry_fill_beyond_the_threshold_warns():
    ledger = FakeLedger(buys=[{"id": 1, "ticker": "AAA", "order_id": "o1",
                               "entry_price": 100.0}])
    orders = FakeOrders(orders={"o1": {"status": "filled", "filled_avg_price": 102.0}})
    rec, ledger, notifier = make(orders=orders, ledger=ledger, slippage_warn_pct=0.5)

    rec.settle_entry_fills()
    assert notifier.severities() == ["WARN"]
    assert "+2.00%" in notifier.alerts[0][1]


def test_dead_entry_order_is_marked_unfilled_not_deleted():
    ledger = FakeLedger(buys=[{"id": 1, "ticker": "AAA", "order_id": "o1",
                               "entry_price": 100.0}])
    orders = FakeOrders(orders={"o1": {"status": "canceled", "filled_avg_price": None}})
    rec, ledger, notifier = make(orders=orders, ledger=ledger)

    result = rec.settle_entry_fills()
    assert result.failed == 1
    assert ledger.buy_fills == [(1, None, None, "UNFILLED")]
    assert notifier.severities() == ["WARN"]


def test_failed_lookup_leaves_the_row_pending():
    """A lookup error is not a verdict — nothing is written and the row is
    retried on the next pass."""
    ledger = FakeLedger(buys=[{"id": 1, "ticker": "AAA", "order_id": "o1",
                               "entry_price": 100.0}])
    rec, ledger, notifier = make(orders=FakeOrders(orders={}), ledger=ledger)

    result = rec.settle_entry_fills()
    assert (result.settled, result.failed, result.pending) == (0, 0, 1)
    assert ledger.buy_fills == []


def test_a_working_order_is_still_pending():
    ledger = FakeLedger(buys=[{"id": 1, "ticker": "AAA", "order_id": "o1",
                               "entry_price": 100.0}])
    orders = FakeOrders(orders={"o1": {"status": "partially_filled",
                                       "filled_avg_price": 100.1}})
    rec, ledger, _ = make(orders=orders, ledger=ledger)

    assert rec.settle_entry_fills().pending == 1
    assert ledger.buy_fills == []


# ── pending exits ──────────────────────────────────────────────────────────


def test_pending_exit_settles_with_realized_pnl():
    ledger = FakeLedger(sells=[{"id": 7, "ticker": "AAA", "order_id": "s1",
                                "entry_price": 100.0, "shares": 10}])
    orders = FakeOrders(orders={"s1": {"status": "filled", "filled_avg_price": 95.0}})
    rec, ledger, notifier = make(orders=orders, ledger=ledger)

    assert rec.settle_pending_exits().settled == 1
    assert ledger.sell_fills == [(7, 95.0, -5.0)]
    assert notifier.severities() == ["INFO"]


def test_dead_liquidation_order_pages_about_an_unprotected_position():
    ledger = FakeLedger(sells=[{"id": 7, "ticker": "AAA", "order_id": "s1",
                                "entry_price": 100.0, "shares": 10}])
    orders = FakeOrders(orders={"s1": {"status": "rejected", "filled_avg_price": None}})
    rec, ledger, notifier = make(orders=orders, ledger=ledger)

    result = rec.settle_pending_exits()
    assert ledger.failed_sells == [7]
    assert result.unresolved == ["AAA"]
    severity, message = notifier.alerts[0]
    assert severity == "CRITICAL"
    assert "UNPROTECTED" in message


# ── broker-side exits ──────────────────────────────────────────────────────


def test_broker_side_exit_is_journaled_against_the_actual_entry_fill():
    """The stop-fill case: nothing in the agent's loop ran, so nothing
    journaled the exit. P&L is measured from the entry's real fill."""
    ledger = FakeLedger(unclosed=[{"ticker": "AAA", "timestamp": "2026-07-01T13:31:00",
                                   "shares": 100, "entry_price": 100.0,
                                   "filled_avg_price": 101.0}])
    orders = FakeOrders(sells={"AAA": {"id": "stop-1", "filled_avg_price": 96.0,
                                       "filled_at": "2026-07-02T15:00:00",
                                       "order_type": "stop"}})
    rec, ledger, notifier = make(broker=FakeBroker(positions=[]), orders=orders,
                                 ledger=ledger)

    assert rec.reconcile_broker_exits().settled == 1
    trade = ledger.logged_trades[0]
    assert trade["side"] == "SELL"
    assert trade["entry_price"] == 101.0                 # actual fill, not decision
    assert trade["pnl_pct"] == pytest.approx(-4.95, abs=0.01)
    assert trade["order_id"] == "stop-1"
    assert ledger.logged_events[0]["event_type"] == "EXIT"
    assert notifier.severities() == ["WARN"]
    assert orders.last_since == "2026-07-01T13:31:00"    # searched from the entry


def test_entry_fill_falls_back_to_the_decision_price():
    ledger = FakeLedger(unclosed=[{"ticker": "AAA", "timestamp": "2026-07-01T13:31:00",
                                   "shares": 10, "entry_price": 100.0,
                                   "filled_avg_price": None}])
    orders = FakeOrders(sells={"AAA": {"id": "s", "filled_avg_price": 90.0,
                                       "filled_at": "x", "order_type": "stop"}})
    rec, ledger, _ = make(broker=FakeBroker([]), orders=orders, ledger=ledger)

    rec.reconcile_broker_exits()
    assert ledger.logged_trades[0]["pnl_pct"] == pytest.approx(-10.0)


def test_a_position_the_broker_still_holds_is_left_alone():
    ledger = FakeLedger(unclosed=[{"ticker": "AAA", "timestamp": "2026-07-01T13:31:00",
                                   "shares": 10, "entry_price": 100.0}])
    rec, ledger, notifier = make(broker=FakeBroker([{"ticker": "AAA", "shares": 10}]),
                                 ledger=ledger)

    assert rec.reconcile_broker_exits().settled == 0
    assert ledger.logged_trades == []
    assert notifier.alerts == []


def test_a_vanished_position_with_no_matching_sell_pages_instead_of_guessing():
    ledger = FakeLedger(unclosed=[{"ticker": "AAA", "timestamp": "2026-07-01T13:31:00",
                                   "shares": 10, "entry_price": 100.0}])
    rec, ledger, notifier = make(broker=FakeBroker([]), orders=FakeOrders(),
                                 ledger=ledger)

    result = rec.reconcile_broker_exits()
    assert result.settled == 0 and result.unresolved == ["AAA"]
    assert ledger.logged_trades == []
    assert notifier.severities() == ["CRITICAL"]


# ── slippage decomposition ─────────────────────────────────────────────────


def test_decomposition_splits_the_gap_from_the_execution_cost():
    """Decision 100 → open 105 (the strategy's gap) → fill 105.2 (execution)."""
    ledger = FakeLedger(decompose=[{"id": 3, "ticker": "AAA",
                                    "timestamp": "2026-07-01T13:31:00",
                                    "entry_price": 100.0, "filled_avg_price": 105.2}])
    orders = FakeOrders(opens={("AAA", "2026-07-01"): 105.0})
    rec, ledger, _ = make(orders=orders, ledger=ledger)

    assert rec.decompose_fills().settled == 1
    trade_id, gap, execution = ledger.decompositions[0]
    assert (trade_id, gap) == (3, 5.0)
    assert execution == pytest.approx(0.19, abs=0.01)


def test_missing_opening_print_leaves_the_fill_for_the_next_pass():
    ledger = FakeLedger(decompose=[{"id": 3, "ticker": "AAA",
                                    "timestamp": "2026-07-01T13:31:00",
                                    "entry_price": 100.0, "filled_avg_price": 105.2}])
    rec, ledger, _ = make(orders=FakeOrders(opens={}), ledger=ledger)

    assert rec.decompose_fills().pending == 1
    assert ledger.decompositions == []


# ── scheduling ─────────────────────────────────────────────────────────────


def test_run_all_isolates_a_failing_pass():
    ledger = FakeLedger(buys=[{"id": 1, "ticker": "AAA", "order_id": "o1",
                               "entry_price": 100.0}])
    orders = FakeOrders(orders={"o1": {"status": "filled", "filled_avg_price": 100.0}})

    class ExplodingBroker:
        def get_positions(self):
            raise ConnectionError("broker history down")

    rec, ledger, notifier = make(broker=ExplodingBroker(), orders=orders, ledger=ledger)
    summary = rec.run_all(decompose=False)

    assert summary["entry_fills"]["settled"] == 1          # ran anyway
    assert "broker history down" in summary["broker_exits"]["error"]
    assert ("WARN", "Reconciliation pass broker_exits failed: broker history down") \
        in notifier.alerts


def test_run_all_can_skip_decomposition_for_the_intraday_run():
    rec, _, _ = make()
    assert set(rec.run_all(decompose=False)) == {"entry_fills", "pending_exits",
                                                 "broker_exits"}
    assert "decomposition" in rec.run_all()


# ── live-vs-backtest comparison ────────────────────────────────────────────


class FakeAnalysisLedger:
    def __init__(self, trips, exec_slips=(), unfilled=0):
        self.trips = trips
        self.exec_slips = list(exec_slips)
        self.unfilled = unfilled

    def get_closed_round_trips(self, days):
        return self.trips

    def get_execution_stats(self, days):
        return {"exec_slippage_pcts": self.exec_slips, "gap_pcts": [],
                "slippage_pcts": [], "unfilled_orders": self.unfilled}


def trips(rs):
    return [{"ticker": "AAA", "timestamp": "t", "pnl_pct": r, "r": r} for r in rs]


def test_small_sample_reports_numbers_without_a_verdict():
    report = expectancy_report(FakeAnalysisLedger(trips([1.0, -1.0, 2.0])))
    assert report["closed_trades"] == 3
    assert report["drift_detected"] is False
    assert "informational only" in report["note"]


def test_drift_is_flagged_once_the_sample_is_large_enough():
    report = expectancy_report(FakeAnalysisLedger(trips([-1.0] * 25)))
    assert report["drift_detected"] is True
    assert report["win_rate_pct"] == 0.0
    assert any("win rate" in i for i in report["issues"])
    assert any("expectancy" in i for i in report["issues"])


def test_a_healthy_large_sample_is_within_tolerance():
    report = expectancy_report(FakeAnalysisLedger(trips([2.0] * 13 + [-1.0] * 12)))
    assert report["drift_detected"] is False
    assert report["avg_r"] > 0
    assert "note" not in report


def test_execution_gate_reads_the_median_not_the_average():
    """One terrible fill should not condemn otherwise-normal execution."""
    slips = [0.05, 0.02, 0.10, 0.03, 9.0]        # mean 1.84%, median 0.05%
    report = expectancy_report(FakeAnalysisLedger(trips([1.0]), exec_slips=slips))
    assert report["median_exec_slippage_pct"] == 0.05
    assert report["drift_detected"] is False


def test_persistently_expensive_execution_is_flagged():
    report = expectancy_report(FakeAnalysisLedger(trips([1.0]), exec_slips=[0.9] * 6))
    assert report["drift_detected"] is True
    assert "median execution slippage" in report["issues"][0]


def test_thin_execution_sample_is_not_judged():
    report = expectancy_report(FakeAnalysisLedger(trips([1.0]), exec_slips=[5.0, 5.0]))
    assert report["median_exec_slippage_pct"] == 5.0
    assert report["drift_detected"] is False


def test_tolerances_are_overridable_and_defaults_untouched():
    ledger = FakeAnalysisLedger(trips([0.5] * 21))
    strict = expectancy_report(ledger, tolerances={"min_avg_r": 1.0})
    assert strict["drift_detected"] is True
    assert expectancy_report(ledger)["drift_detected"] is False
    assert DEFAULT_TOLERANCES["min_avg_r"] == 0.0


def test_unfilled_orders_are_surfaced_in_the_report():
    report = expectancy_report(FakeAnalysisLedger(trips([1.0]), unfilled=4))
    assert report["unfilled_orders"] == 4


def test_empty_window_reports_nothing_rather_than_dividing_by_zero():
    report = expectancy_report(FakeAnalysisLedger([]))
    assert report["closed_trades"] == 0
    assert report["win_rate_pct"] is None and report["avg_r"] is None
    assert report["drift_detected"] is False
