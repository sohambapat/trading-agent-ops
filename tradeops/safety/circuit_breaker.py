"""Portfolio circuit breaker — code-enforced, outside the agent's learnable surface.

A self-improving agent rewrites its own strategy and prompt configuration.
The one thing it must never be able to rewrite is the kill switch: these
thresholds live in code and environment only, and the learning loop is never
told they exist.

Halt levels:
  0  normal
  1  soft halt  — daily loss or losing streak; no new entries TODAY (auto-expires)
  2  hard halt  — drawdown from high-water mark; no entries until manual reset
  3  flatten    — drawdown breach; close everything, cancel orders, halt

Fail-closed for NEW ENTRIES only: if equity can't be fetched or halt state is
unreadable, entries are blocked. Position management (open-position review,
protective-stop maintenance) must never consult this module — exits and stop
maintenance stay available even when the breaker is tripped.
"""
import json
import os
from datetime import datetime, timezone

from tradeops.interfaces import Broker, Journal, Notifier

# EXAMPLE thresholds — derive real values from your strategy's backtested
# worst-case drawdown, with enough margin that the breaker only fires on
# genuine failure, and derate them for live capital.
DEFAULT_THRESHOLDS = {
    "paper": {"l1_daily_loss_pct": 3.0, "l2_drawdown_pct": 12.0, "l3_drawdown_pct": 18.0},
    "live":  {"l1_daily_loss_pct": 2.0, "l2_drawdown_pct": 8.0,  "l3_drawdown_pct": 12.0},
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _today() -> str:
    return _utcnow().date().isoformat()


class CircuitBreaker:
    def __init__(self, broker: Broker, journal: Journal, notifier: Notifier,
                 state_path: str,
                 phase: str | None = None,
                 thresholds: dict | None = None,
                 losing_days_halt: int = 3,
                 streak_min_loss_pct: float = 0.5):
        self.broker = broker
        self.journal = journal
        self.notifier = notifier
        self.state_path = state_path
        phase = (phase or os.getenv("BREAKER_PHASE", "paper")).lower()
        self.th = thresholds or DEFAULT_THRESHOLDS.get(phase, DEFAULT_THRESHOLDS["paper"])
        self.losing_days_halt = losing_days_halt
        # Magnitude floor for the losing-day streak: a streak day must lose at
        # least this % of the prior day's equity. A magnitude-blind newer<older
        # rule trips benign halts on sub-0.1% equity drifts (holiday marks,
        # rounding) — a losing STREAK requires material losing DAYS.
        self.streak_min_loss_pct = streak_min_loss_pct

    # ── halt state persistence ─────────────────────────────────────────────

    @staticmethod
    def _default_state() -> dict:
        return {
            "level": 0, "reason": "", "triggered_at": None, "trigger_date": None,
            "daily_loss_pct": None, "drawdown_pct": None, "high_water_mark": None,
            "requires_manual_reset": False, "fail_closed": False,
            "reset_by": None, "last_eval": None,
        }

    def _read_halt_state(self) -> dict | None:
        """None means unreadable/corrupt (distinct from missing = fresh default)."""
        if not os.path.exists(self.state_path):
            return self._default_state()
        try:
            with open(self.state_path) as f:
                state = json.load(f)
            if not isinstance(state.get("level"), int):
                return None
            return state
        except Exception:
            return None

    def _write_halt_state(self, state: dict):
        state["last_eval"] = _utcnow().isoformat()
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, self.state_path)

    # ── core logic ─────────────────────────────────────────────────────────

    def record_equity_snapshot(self, source: str) -> dict | None:
        """Fetch account equity and persist a snapshot. None on broker failure."""
        try:
            p = self.broker.get_portfolio()
        except Exception as e:
            self.notifier.send_alert(f"Equity snapshot failed ({source}): {e}", "WARN")
            return None
        self.journal.log_equity_snapshot(
            p["total_value"], p.get("cash"), p.get("buying_power"), source)
        return p

    def evaluate(self, equity: float | None) -> dict:
        """Pure evaluation against history/HWM. No alerts, no state writes."""
        result = {"level": 0, "daily_loss_pct": None, "drawdown_pct": None,
                  "hwm": None, "losing_days": 0, "fail_closed": False, "reason": ""}
        if equity is None or equity <= 0:
            result.update(level=1, fail_closed=True,
                          reason="equity unavailable — failing closed for new entries")
            return result

        history = self.journal.get_equity_history(days=self.losing_days_halt + 2)
        hwm = self.journal.get_high_water_mark()
        result["hwm"] = hwm

        # daily loss: latest equity vs previous day's last snapshot
        prev = next((h for h in history if h["date"] < _today()), None)
        if prev and prev["equity"]:
            result["daily_loss_pct"] = round((equity - prev["equity"]) / prev["equity"] * 100, 2)

        # losing streak: consecutive MATERIAL down days in the daily series
        # (incl. today) — each must lose >= streak_min_loss_pct of the prior day
        series = [equity] + [h["equity"] for h in history if h["date"] < _today()]
        losing = 0
        for newer, older in zip(series, series[1:]):
            if older and newer < older and (older - newer) / older * 100 >= self.streak_min_loss_pct:
                losing += 1
            else:
                break
        result["losing_days"] = losing

        if hwm and hwm > 0:
            result["drawdown_pct"] = round((hwm - equity) / hwm * 100, 2)

        dd = result["drawdown_pct"] or 0.0
        if dd >= self.th["l3_drawdown_pct"]:
            result.update(level=3, reason=f"drawdown {dd:.1f}% >= flatten threshold "
                                          f"{self.th['l3_drawdown_pct']}%")
        elif dd >= self.th["l2_drawdown_pct"]:
            result.update(level=2, reason=f"drawdown {dd:.1f}% >= hard-halt threshold "
                                          f"{self.th['l2_drawdown_pct']}%")
        elif (result["daily_loss_pct"] is not None
              and result["daily_loss_pct"] <= -self.th["l1_daily_loss_pct"]):
            result.update(level=1, reason=f"daily loss {result['daily_loss_pct']:.1f}% >= "
                                          f"{self.th['l1_daily_loss_pct']}% limit")
        elif losing >= self.losing_days_halt:
            result.update(level=1, reason=f"{losing} consecutive losing days (equity-based)")
        return result

    def check_and_update(self, source: str = "scheduled") -> dict:
        """Snapshot equity, evaluate, persist transitions, alert, flatten on L3.
        Returns the evaluation merged with the persisted state."""
        portfolio = self.record_equity_snapshot(source)
        equity = portfolio["total_value"] if portfolio else None
        ev = self.evaluate(equity)

        state = self._read_halt_state()
        if state is None:
            self.notifier.send_alert(
                "halt state unreadable — failing closed for new entries", "CRITICAL")
            ev["fail_closed"] = True
            return ev
        old_level = state.get("level", 0)

        # latched L2/L3 never auto-clears; expired L1 clears; new trips escalate
        today = _today()
        if state.get("requires_manual_reset"):
            effective = max(old_level, ev["level"])
        elif old_level == 1 and state.get("trigger_date") == today:
            effective = max(1, ev["level"])
        else:
            effective = ev["level"]

        if effective != old_level:
            if effective > old_level:
                self.journal.log_breaker_event(
                    f"TRIP_L{effective}", old_level, effective, equity,
                    ev["daily_loss_pct"], ev["drawdown_pct"], ev["hwm"], ev["reason"])
                self.notifier.send_alert(
                    f"CIRCUIT BREAKER L{effective}: {ev['reason']}", "CRITICAL")
            state.update(
                level=effective, reason=ev["reason"] if effective else "",
                triggered_at=_utcnow().isoformat() if effective > old_level
                             else state.get("triggered_at"),
                trigger_date=today if effective > old_level else state.get("trigger_date"),
                daily_loss_pct=ev["daily_loss_pct"], drawdown_pct=ev["drawdown_pct"],
                high_water_mark=ev["hwm"],
                requires_manual_reset=effective >= 2
                    or state.get("requires_manual_reset", False) and effective > 0,
            )

        state["fail_closed"] = ev["fail_closed"]
        if ev["fail_closed"]:
            self.journal.log_breaker_event("FAIL_CLOSED", old_level, state["level"],
                                           equity, reason=ev["reason"])
        self._write_halt_state(state)

        if effective == 3 and old_level < 3:
            self.flatten_all()

        ev["level"] = state["level"]
        ev["requires_manual_reset"] = state.get("requires_manual_reset", False)
        return ev

    def flatten_all(self) -> dict:
        """Emergency liquidation: cancel every open order first (resting stops
        hold the shares — closes are rejected otherwise), then market-close
        each position. Never raises; reports per-ticker results."""
        results = {"cancelled": [], "closed": [], "failed": []}
        try:
            orders = self.broker.get_open_orders()
        except Exception as e:
            orders = []
            results["failed"].append(f"order fetch: {e}")
        cancelled_ids = []
        for o in orders:
            try:
                self.broker.cancel_order(o["id"])
                cancelled_ids.append(o["id"])
                results["cancelled"].append(o["ticker"])
            except Exception as e:
                results["failed"].append(f"cancel {o['ticker']}: {e}")
        # cancels are async — wait for each to settle before liquidating
        for oid in cancelled_ids:
            self.broker.wait_order_terminal(oid)

        try:
            positions = self.broker.get_positions()
        except Exception as e:
            positions = []
            results["failed"].append(f"position fetch: {e}")
        for pos in positions:
            try:
                self.broker.close_position(pos["ticker"])
                exit_px = pos.get("current_price")
                pnl_pct = (round((exit_px - pos["avg_cost"]) / pos["avg_cost"] * 100, 2)
                           if exit_px and pos.get("avg_cost") else None)
                self.journal.log_trade(
                    pos["ticker"], "SELL", entry_price=pos.get("avg_cost"),
                    shares=pos.get("shares"), exit_price=exit_px, pnl_pct=pnl_pct,
                    reasoning="circuit breaker L3 flatten")
                results["closed"].append(pos["ticker"])
            except Exception as e:
                results["failed"].append(f"close {pos['ticker']}: {e}")

        self.notifier.send_alert(
            f"FLATTEN: cancelled={results['cancelled']} closed={results['closed']} "
            f"failed={results['failed'] or 'none'}", "CRITICAL")
        return results

    def entries_allowed(self) -> tuple[bool, str]:
        """Last-line gate before any new entry. Reads halt state only (no network)."""
        state = self._read_halt_state()
        if state is None:
            self.notifier.send_alert("halt state unreadable — blocking new entries", "WARN")
            return False, "halt state unreadable — fail closed"
        if state.get("fail_closed"):
            return False, "fail-closed: " + (state.get("reason") or "equity unavailable")
        level = state.get("level", 0)
        if level >= 2:
            return False, (f"hard halt L{level} active: {state.get('reason', '')} "
                           f"(manual reset required)")
        if level == 1 and state.get("trigger_date") == _today():
            return False, f"soft halt active today: {state.get('reason', '')}"
        return True, "no active halt"

    def manual_reset(self, operator_note: str) -> dict:
        state = self._read_halt_state() or self._default_state()
        old_level = state.get("level", 0)
        fresh = self._default_state()
        self._write_halt_state(fresh)
        self.journal.log_breaker_event("MANUAL_RESET", old_level, 0,
                                       reason=operator_note, operator="admin")
        self.notifier.send_alert(
            f"Circuit breaker manually reset (was L{old_level}): {operator_note}", "INFO")
        return fresh

    def run_drill(self) -> dict:
        """Prove the halt path end-to-end without touching positions: force L2,
        verify entries are blocked, restore prior state, log a DRILL row
        (go/no-go gate evidence)."""
        before = self._read_halt_state() or self._default_state()
        forced = dict(self._default_state(), level=2, reason="breaker drill",
                      triggered_at=_utcnow().isoformat(), trigger_date=_today(),
                      requires_manual_reset=True)
        self._write_halt_state(forced)
        allowed, why = self.entries_allowed()
        self._write_halt_state(before)
        passed = allowed is False
        self.journal.log_breaker_event("DRILL", before.get("level", 0),
                                       before.get("level", 0),
                                       reason=f"halt_blocked_entries={passed} ({why})")
        if passed:
            self.notifier.send_alert("Breaker drill passed — L2 halt blocks entries", "INFO")
        else:
            self.notifier.send_alert(
                "Breaker drill FAILED — forced L2 did not block entries", "CRITICAL")
        return {"passed": passed, "detail": why}
