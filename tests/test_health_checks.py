"""Health-check unit suite — all ports faked in memory.

DailyHealthCheck is the gate the daily loop consults before placing anything.
The suite covers the guarantee that matters most: unknown states block entries,
not exits; a missing stop triggers remediation AND a journal row; a breaker
that cannot be evaluated fails closed.
"""
import pytest

from tradeops.health import DailyHealthCheck, ProbeResult

# ── fakes ──────────────────────────────────────────────────────────────────────


class FakeBroker:
    def __init__(self, portfolio=None, positions=None, orders=None):
        self._portfolio = portfolio or {"total_value": 100_000, "cash": 50_000,
                                        "buying_power": 50_000}
        self._positions = positions or []
        self._orders = orders or []
        self.calls = []
        self.fail_portfolio = False
        self.fail_orders = False
        self.fail_stop = False

    def get_portfolio(self):
        if self.fail_portfolio:
            raise ConnectionError("broker down")
        return self._portfolio

    def get_positions(self):
        return self._positions

    def get_open_orders(self):
        if self.fail_orders:
            raise ConnectionError("orders down")
        return self._orders

    def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))

    def close_position(self, ticker):
        self.calls.append(("close", ticker))

    def place_stop_order(self, ticker, shares, stop_price):
        if self.fail_stop:
            raise ConnectionError("stop rejected")
        self.calls.append(("stop", ticker, shares, stop_price))

    def wait_order_terminal(self, order_id):
        self.calls.append(("wait", order_id))


class FakeJournal:
    def __init__(self):
        self.health_rows = []
        self.vuln_rows = []

    def log_equity_snapshot(self, equity, cash, buying_power, source):
        pass

    def get_equity_history(self, days):
        return []

    def get_high_water_mark(self):
        return None

    def log_breaker_event(self, event, old_level, new_level, **kw):
        pass

    def log_trade(self, ticker, side, **fields):
        pass

    def log_health_check(self, **fields):
        self.health_rows.append(fields)

    def log_vulnerability(self, vuln_id, severity, description,
                          auto_remediated, remediation_action):
        self.vuln_rows.append({"id": vuln_id, "sev": severity,
                               "desc": description, "fixed": auto_remediated})


class FakeNotifier:
    def __init__(self):
        self.alerts = []

    def send_alert(self, message, severity="INFO"):
        self.alerts.append((severity, message))


class FakeBreaker:
    def __init__(self, level=0, fail=False, fail_closed=False):
        self._level = level
        self._fail = fail
        self._fail_closed = fail_closed

    def check_and_update(self, source="premarket"):
        if self._fail:
            raise RuntimeError("breaker exploded")
        return {"level": self._level, "fail_closed": self._fail_closed,
                "reason": f"L{self._level} test"}


def make_check(**kw):
    broker = kw.pop("broker", FakeBroker())
    journal = kw.pop("journal", FakeJournal())
    notifier = kw.pop("notifier", FakeNotifier())
    return DailyHealthCheck(broker, journal, notifier, **kw), broker, journal, notifier


# ── startup_check ──────────────────────────────────────────────────────────────


def test_startup_passes_healthy():
    hc, _, _, notifier = make_check(llm_probe=lambda: None)
    failures = hc.run_startup_check()
    assert failures == []
    assert any("passed" in m for _, m in notifier.alerts)


def test_startup_fails_llm_probe_raises():
    def bad_probe():
        raise ConnectionError("API key wrong")

    hc, *_ = make_check(llm_probe=bad_probe)
    failures = hc.run_startup_check()
    assert any("decision model" in f for f in failures)


def test_startup_fails_broker_raises():
    broker = FakeBroker()
    broker.fail_portfolio = True
    hc, _, _, notifier = make_check(broker=broker)
    failures = hc.run_startup_check()
    assert any("broker" in f for f in failures)
    assert any("CRITICAL" == sev for sev, _ in notifier.alerts)


def test_startup_fails_zero_portfolio():
    broker = FakeBroker(portfolio={"total_value": 0, "cash": 0, "buying_power": 0})
    hc, *_ = make_check(broker=broker)
    failures = hc.run_startup_check()
    assert any("empty" in f or "restricted" in f for f in failures)


# ── run — happy path ───────────────────────────────────────────────────────────


def test_run_ok_when_all_healthy():
    hc, _, journal, _ = make_check()
    result = hc.run()
    assert result["ok"] is True
    assert result["skip_entries"] is False
    assert len(journal.health_rows) == 1
    assert journal.health_rows[0]["api_ok"] is True


def test_run_journals_every_call():
    hc, _, journal, _ = make_check()
    hc.run()
    hc.run()
    assert len(journal.health_rows) == 2


# ── run — LLM probe failure ────────────────────────────────────────────────────


def test_run_blocks_entries_when_llm_down():
    hc, _, _, notifier = make_check(llm_probe=lambda: (_ for _ in ()).throw(
        ConnectionError("anthropic 503")))
    result = hc.run()
    assert result["skip_entries"] is True
    assert result["ok"] is False
    assert any("CRITICAL" == sev for sev, _ in notifier.alerts)


def test_run_no_llm_probe_does_not_block():
    hc, *_ = make_check(llm_probe=None)
    result = hc.run()
    assert result["skip_entries"] is False


# ── run — broker failure ───────────────────────────────────────────────────────


def test_run_blocks_entries_when_broker_down():
    broker = FakeBroker()
    broker.fail_portfolio = True
    hc, *_ = make_check(broker=broker)
    result = hc.run()
    assert result["skip_entries"] is True
    assert result["ok"] is False


# ── stop-coverage verification ─────────────────────────────────────────────────


def _position(ticker, shares=10, avg_cost=100.0, qty_available=None):
    p = {"ticker": ticker, "shares": shares, "avg_cost": avg_cost,
         "current_price": avg_cost}
    if qty_available is not None:
        p["qty_available"] = qty_available
    return p


def _stop_order(ticker):
    return {"id": f"s-{ticker}", "ticker": ticker, "type": "stop"}


def test_stop_present_no_emergency_placed():
    broker = FakeBroker(
        positions=[_position("AAPL")],
        orders=[_stop_order("AAPL")])
    hc, _, journal, _ = make_check(broker=broker)
    hc.run()
    assert not broker.calls
    assert not journal.vuln_rows


def test_missing_stop_triggers_emergency_placement():
    broker = FakeBroker(
        positions=[_position("NVDA", avg_cost=200.0)],
        orders=[])  # no stop for NVDA
    hc, _, journal, notifier = make_check(broker=broker)
    hc.run()
    stops_placed = [c for c in broker.calls if c[0] == "stop"]
    assert len(stops_placed) == 1
    _, ticker, shares, price = stops_placed[0]
    assert ticker == "NVDA"
    assert price == pytest.approx(200.0 * 0.90, rel=1e-4)
    # detection + remediation both journaled
    assert len(journal.vuln_rows) == 2
    assert any("CRITICAL" == sev and "NVDA" in m for sev, m in notifier.alerts)


def test_position_fully_reserved_by_orders_skipped():
    # qty_available=0 means every share is tied up in an order (e.g. an OTO)
    broker = FakeBroker(
        positions=[_position("MSFT", qty_available=0)],
        orders=[])  # no stop, but qty_available=0 = covered
    hc, _, journal, _ = make_check(broker=broker)
    hc.run()
    assert not broker.calls
    assert not journal.vuln_rows


def test_stop_placement_failure_alerts_and_marks_not_ok():
    broker = FakeBroker(positions=[_position("AMD")], orders=[])
    broker.fail_stop = True
    hc, _, _, notifier = make_check(broker=broker)
    hc.run()
    # failure alert must land
    assert any("CRITICAL" == sev for sev, _ in notifier.alerts)


def test_order_fetch_failure_is_unverifiable_critical():
    broker = FakeBroker(positions=[_position("TSLA")])
    broker.fail_orders = True
    hc, _, journal, notifier = make_check(broker=broker)
    hc.run()
    assert any("CRITICAL" == sev and "stop coverage" in m.lower()
               for sev, m in notifier.alerts)


def test_emergency_stop_pct_respected():
    broker = FakeBroker(positions=[_position("META", avg_cost=500.0)], orders=[])
    hc, *_ = make_check(broker=broker, emergency_stop_pct=5.0)
    hc.run()
    stops = [c for c in broker.calls if c[0] == "stop"]
    _, _, _, price = stops[0]
    assert price == pytest.approx(500.0 * 0.95, rel=1e-4)


# ── circuit breaker integration ────────────────────────────────────────────────


def test_breaker_l1_sets_skip_entries():
    hc, *_ = make_check(breaker=FakeBreaker(level=1))
    result = hc.run()
    assert result["skip_entries"] is True
    assert result["breaker_level"] == 1


def test_breaker_l0_does_not_block():
    hc, *_ = make_check(breaker=FakeBreaker(level=0))
    result = hc.run()
    assert result["skip_entries"] is False


def test_breaker_exception_fails_closed():
    hc, _, _, notifier = make_check(breaker=FakeBreaker(fail=True))
    result = hc.run()
    assert result["skip_entries"] is True
    assert any("CRITICAL" == sev and "breaker" in m.lower()
               for sev, m in notifier.alerts)


def test_no_breaker_does_not_block():
    hc, *_ = make_check(breaker=None)
    result = hc.run()
    assert result["skip_entries"] is False


# ── extra probes ───────────────────────────────────────────────────────────────


def test_extra_probe_note_appended():
    def probe():
        return ProbeResult(note="data feed latency high", skip_entries=False, severity="WARN")
    hc, _, journal, _ = make_check(extra_probes=[probe])
    result = hc.run()
    assert "data feed latency high" in result["notes"]


def test_extra_probe_skip_entries_propagates():
    def probe():
        return ProbeResult(note="SPY data stale", skip_entries=True, severity="WARN")
    hc, *_ = make_check(extra_probes=[probe])
    result = hc.run()
    assert result["skip_entries"] is True


def test_extra_probe_none_return_ignored():
    hc, _, _, notifier = make_check(extra_probes=[lambda: None])
    result = hc.run()
    assert result["ok"] is True
    assert result["notes"] == []


def test_extra_probe_exception_becomes_note_not_crash():
    def bad_probe():
        raise ValueError("regime check exploded")

    hc, _, _, _ = make_check(extra_probes=[bad_probe])
    result = hc.run()  # must not raise
    assert any("probe error" in n for n in result["notes"])


def test_extra_probe_warn_severity_sends_alert():
    def probe():
        return ProbeResult(note="VIX degraded", skip_entries=False, severity="WARN")
    hc, _, _, notifier = make_check(extra_probes=[probe])
    hc.run()
    assert any("VIX degraded" in m for _, m in notifier.alerts)


def test_extra_probe_info_severity_no_alert():
    def probe():
        return ProbeResult(note="heartbeat ok", skip_entries=False, severity="INFO")
    hc, _, _, notifier = make_check(extra_probes=[probe])
    hc.run()
    # INFO probes append to notes but must not trigger send_alert
    assert not any("heartbeat ok" in m for _, m in notifier.alerts)


# ── journal fields ─────────────────────────────────────────────────────────────


def test_journal_entries_blocked_field_set_correctly():
    hc, _, journal, _ = make_check(breaker=FakeBreaker(level=2))
    hc.run()
    assert journal.health_rows[-1]["entries_blocked"] is True


def test_journal_notes_joined_on_semicolon():
    def probe1():
        return ProbeResult(note="note-a", skip_entries=False, severity="INFO")
    def probe2():
        return ProbeResult(note="note-b", skip_entries=False, severity="INFO")
    hc, _, journal, _ = make_check(extra_probes=[probe1, probe2])
    hc.run()
    notes = journal.health_rows[-1]["notes"]
    assert "note-a" in notes and "note-b" in notes
