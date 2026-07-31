"""Pre-market daily health check — the gate a trading loop reads before it
places anything.

The check answers one question: is it safe to let an autonomous agent trade
today? It probes the decision model and the broker, verifies every open
position still has a protective stop resting AT THE BROKER (placing an
emergency stop when one is missing), consults the circuit breaker, and
journals the result. Failures bias one way: anything that cannot be verified
blocks NEW ENTRIES for the day; nothing here ever blocks position management.

The stop-coverage check earns its keep. Resting stops can vanish for reasons
that have nothing to do with price: a time-in-force mismatch expiring stop
legs at the close, or a holiday-blind scheduler canceling stops against a
closed market that rejects their replacements. Coverage is therefore verified
against the broker's live order book every morning, assuming any stop can be
missing for any reason — and both the detection and the remediation leave
journal rows, because a go/no-go gate later counts unremediated criticals.
"""
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from tradeops.interfaces import Broker, Journal, Notifier

# EXAMPLE distance — production derives this from strategy geometry. It only
# needs to be wide enough that the emergency stop is a backstop against
# disaster, not a substitute for the strategy's own exit levels.
DEFAULT_EMERGENCY_STOP_PCT = 10.0


@dataclass
class ProbeResult:
    """Outcome of an injected extra probe (market-regime checks, config
    integrity, data-feed liveness — whatever the composition wires in)."""
    note: str
    skip_entries: bool = False
    severity: str = "WARN"


class DailyHealthCheck:
    def __init__(self, broker: Broker, journal: Journal, notifier: Notifier,
                 breaker=None,
                 llm_probe: Callable[[], None] | None = None,
                 extra_probes: Iterable[Callable[[], "ProbeResult | None"]] = (),
                 emergency_stop_pct: float = DEFAULT_EMERGENCY_STOP_PCT):
        self.broker = broker
        self.journal = journal
        self.notifier = notifier
        self.breaker = breaker  # anything with check_and_update(source=) -> dict
        self.llm_probe = llm_probe
        self.extra_probes = list(extra_probes)
        self.emergency_stop_pct = emergency_stop_pct

    # ── startup ────────────────────────────────────────────────────────────

    def run_startup_check(self) -> list[str]:
        """Once at process start. Returns failure strings; empty means
        healthy. The caller decides whether to exit — production does."""
        failures = []
        if self.llm_probe is not None:
            try:
                self.llm_probe()
            except Exception as e:
                failures.append(f"decision model unreachable: {e}")
        try:
            portfolio = self.broker.get_portfolio()
            if portfolio["total_value"] <= 0:
                failures.append("broker account empty or restricted")
        except Exception as e:
            failures.append(f"broker unreachable: {e}")

        for failure in failures:
            self.notifier.send_alert(failure, "CRITICAL")
        if not failures:
            self.notifier.send_alert("Startup check passed — agent starting", "INFO")
        return failures

    # ── the daily pre-market pass ──────────────────────────────────────────

    def run(self, source: str = "premarket") -> dict:
        result = {"ok": True, "skip_entries": False, "breaker_level": 0, "notes": []}
        api_ok = True
        stops_ok = True

        # decision model: an agent that can't think must not enter
        if self.llm_probe is not None:
            try:
                self.llm_probe()
            except Exception as e:
                self.notifier.send_alert(f"Decision model down at pre-market: {e}",
                                         "CRITICAL")
                api_ok = False
                result["ok"] = False
                result["skip_entries"] = True

        # broker reachability
        positions = []
        try:
            self.broker.get_portfolio()
            positions = self.broker.get_positions()
        except Exception as e:
            self.notifier.send_alert(f"Broker down at pre-market: {e}", "CRITICAL")
            api_ok = False
            result["ok"] = False
            result["skip_entries"] = True

        # protective-stop coverage (remediates, never blocks entries by itself)
        if positions:
            stops_ok = self._verify_stop_coverage(positions, result["notes"])

        # circuit breaker — a breaker that cannot be evaluated fails CLOSED
        if self.breaker is not None:
            try:
                ev = self.breaker.check_and_update(source=source)
                result["breaker_level"] = ev.get("level", 0)
                if ev.get("level", 0) >= 1 or ev.get("fail_closed"):
                    result["skip_entries"] = True
                    result["notes"].append(
                        f"circuit breaker L{ev.get('level', '?')}: {ev.get('reason', '')}")
            except Exception as e:
                result["skip_entries"] = True
                result["notes"].append(f"breaker check failed — failing closed: {e}")
                self.notifier.send_alert(
                    f"Breaker check failed — blocking new entries: {e}", "CRITICAL")

        # composition-specific probes (a probe error is a note, never a crash)
        for probe in self.extra_probes:
            try:
                probe_result = probe()
            except Exception as e:
                probe_result = ProbeResult(note=f"probe error: {e}")
            if probe_result is None:
                continue
            result["notes"].append(probe_result.note)
            if probe_result.skip_entries:
                result["skip_entries"] = True
            if probe_result.severity != "INFO":
                self.notifier.send_alert(probe_result.note, probe_result.severity)

        self.journal.log_health_check(
            api_ok=api_ok, stops_ok=stops_ok,
            breaker_level=result["breaker_level"],
            entries_blocked=result["skip_entries"],
            notes="; ".join(result["notes"]),
        )
        return result

    def _verify_stop_coverage(self, positions: list[dict], notes: list[str]) -> bool:
        try:
            open_orders = self.broker.get_open_orders()
        except Exception as e:
            notes.append(f"stop coverage unverifiable: {e}")
            self.notifier.send_alert(
                f"Cannot verify stop coverage — order fetch failed: {e}", "CRITICAL")
            return False

        stop_tickers = {o["ticker"] for o in open_orders
                        if "stop" in o.get("type", "").lower()}
        ok = True
        for pos in positions:
            ticker = pos["ticker"]
            # every share already reserved by a resting sell order = covered
            if pos.get("qty_available", pos.get("shares", 0)) == 0:
                continue
            if ticker in stop_tickers:
                continue
            self.journal.log_vulnerability(
                "STOP-COVERAGE", "CRITICAL",
                f"missing stop order for {ticker}", False, "")
            self.notifier.send_alert(
                f"Missing stop order for {ticker} — placing emergency stop", "CRITICAL")
            try:
                stop_price = round(
                    pos["avg_cost"] * (1 - self.emergency_stop_pct / 100), 2)
                self.broker.place_stop_order(ticker, int(pos["shares"]), stop_price)
                self.journal.log_vulnerability(
                    "STOP-COVERAGE", "CRITICAL",
                    f"emergency stop placed for {ticker} @ {stop_price}",
                    True, f"stop @ {stop_price}")
                notes.append(f"emergency stop placed for {ticker}")
            except Exception as e:
                ok = False
                self.notifier.send_alert(
                    f"Failed to place emergency stop for {ticker}: {e}", "CRITICAL")
        return ok
