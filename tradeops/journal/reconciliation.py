"""Reconciliation — making the journal agree with the broker, then making the
live record comparable to the backtest.

An agent's journal is written at decision time; the broker's record is written
at fill time. Everything in between is where a performance record quietly
becomes fiction:

- An order is placed and journaled, then fills at a different price.
- An exit is decided before the open and cannot fill until the auction.
- A resting stop fills autonomously overnight. Nothing in the agent's loop
  ran, so nothing journaled it — and since a stop fills on the way *down*,
  the rows that go missing this way are almost all losses. Left alone, the
  journal's expectancy drifts optimistic on its own.

`FillReconciler` runs four passes, each independent and each safe to re-run.
They execute after the open and again at the close, because a fill can land
between the two and the day's numbers should be true by the time anything
reads them.

The guiding rule: **a failed lookup is not a verdict.** Anything that cannot
be resolved stays pending and is retried on the next pass; rows age out of the
retry window rather than being resolved by guesswork.
"""
import statistics
from dataclasses import dataclass, field

from tradeops.interfaces import Broker, Notifier, OrderHistory, TradeLedger

# Terminal order states. A broker that reports one of these has given a real
# verdict; anything else (new, accepted, partially_filled, pending_*) means
# "not yet" and leaves the row pending.
DEAD_ORDER_STATES = ("canceled", "cancelled", "expired", "rejected")

# EXAMPLE thresholds — production derives these from its own backtest.
DEFAULT_SLIPPAGE_WARN_PCT = 0.5


@dataclass
class PassResult:
    """What one reconciliation pass did. Returned rather than logged so a
    scheduler can assert on it and a test can read it."""
    settled: int = 0
    failed: int = 0
    pending: int = 0
    unresolved: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"settled": self.settled, "failed": self.failed,
                "pending": self.pending, "unresolved": list(self.unresolved)}


class FillReconciler:
    def __init__(self, broker: Broker, orders: OrderHistory, ledger: TradeLedger,
                 notifier: Notifier,
                 slippage_warn_pct: float = DEFAULT_SLIPPAGE_WARN_PCT,
                 pending_days: int = 3, decomposition_days: int = 5):
        self.broker = broker
        self.orders = orders
        self.ledger = ledger
        self.notifier = notifier
        self.slippage_warn_pct = slippage_warn_pct
        self.pending_days = pending_days
        self.decomposition_days = decomposition_days

    # ── pass 1: entry fills ────────────────────────────────────────────────

    def settle_entry_fills(self) -> PassResult:
        """Record what entries actually cost.

        An unfilled entry is marked UNFILLED rather than deleted: downstream
        analysis needs to know the difference between "the strategy passed"
        and "the strategy wanted this trade and did not get it".
        """
        result = PassResult()
        for row in self.ledger.get_pending_buy_fills(self.pending_days):
            order = self.orders.get_order(row["order_id"])
            if order is None:
                result.pending += 1
                continue
            status = (order.get("status") or "").lower()
            fill = order.get("filled_avg_price")
            if status == "filled" and fill:
                decision = row.get("entry_price") or 0
                slippage = (round((fill - decision) / decision * 100, 3)
                            if decision else None)
                self.ledger.set_buy_fill(row["id"], fill, slippage, "FILLED")
                result.settled += 1
                if slippage is not None and slippage > self.slippage_warn_pct:
                    self.notifier.send_alert(
                        f"Slippage on {row['ticker']}: filled {fill:.2f} vs decision "
                        f"{decision:.2f} ({slippage:+.2f}%)", "WARN")
            elif status in DEAD_ORDER_STATES:
                self.ledger.set_buy_fill(row["id"], fill_status="UNFILLED")
                result.failed += 1
                self.notifier.send_alert(
                    f"{row['ticker']}: entry order {status} — no position opened", "WARN")
            else:
                result.pending += 1
        return result

    # ── pass 2: pending exits ──────────────────────────────────────────────

    def settle_pending_exits(self) -> PassResult:
        """Settle exits journaled before their fill existed.

        The failure branch pages CRITICAL and says why: the exit path cancels
        the protective stop before submitting the liquidating order, so an
        order that dies unfilled leaves a position held *and* unprotected.
        That is the one state in this module a human has to resolve.
        """
        result = PassResult()
        for row in self.ledger.get_pending_sell_fills(self.pending_days):
            order = self.orders.get_order(row["order_id"])
            if order is None:
                result.pending += 1
                continue
            status = (order.get("status") or "").lower()
            fill = order.get("filled_avg_price")
            if status == "filled" and fill:
                entry = row.get("entry_price") or 0
                pnl = round((fill - entry) / entry * 100, 2) if entry else None
                self.ledger.settle_sell_fill(row["id"], fill, pnl)
                result.settled += 1
                suffix = f" ({pnl:+.2f}% vs entry)" if pnl is not None else ""
                self.notifier.send_alert(
                    f"{row['ticker']}: exit filled {fill:.2f}{suffix}", "INFO")
            elif status in DEAD_ORDER_STATES:
                self.ledger.mark_sell_failed(row["id"])
                result.failed += 1
                result.unresolved.append(row["ticker"])
                self.notifier.send_alert(
                    f"{row['ticker']}: liquidating order {status} — position may still "
                    f"be HELD and UNPROTECTED (its stop was canceled at exit). "
                    f"Manual intervention required.", "CRITICAL")
            else:
                result.pending += 1
        return result

    # ── pass 3: broker-side exits ──────────────────────────────────────────

    def reconcile_broker_exits(self) -> PassResult:
        """Journal exits the agent never made.

        A position that the journal calls open and the broker does not hold was
        closed by something outside the loop — nearly always a resting stop.
        The fill is found by searching the broker's closed sell orders for that
        ticker after the entry timestamp, and journaled with the reason it was
        reconstructed.

        Two ordering details keep this honest. It runs *after* pending exits
        settle, so a just-settled exit has already closed its round trip and
        cannot be journaled twice. And P&L is computed against the entry's
        actual fill, falling back to the decision price only when no fill was
        recorded — the average-cost convention the rest of the record uses.

        A position that is gone with no matching sell order is never guessed
        at; it pages.
        """
        result = PassResult()
        held = {p["ticker"] for p in self.broker.get_positions()}
        for entry in self.ledger.get_unclosed_entries():
            ticker = entry["ticker"]
            if ticker in held:
                continue
            sell = self.orders.get_recent_filled_sell(ticker, entry["timestamp"])
            if sell is None:
                result.unresolved.append(ticker)
                self.notifier.send_alert(
                    f"{ticker}: journal shows an open entry but the broker has no "
                    f"position and no filled sell order — reconcile manually",
                    "CRITICAL")
                continue
            basis = entry.get("filled_avg_price") or entry.get("entry_price") or 0
            fill = sell["filled_avg_price"]
            pnl = round((fill - basis) / basis * 100, 2) if basis else None
            self.ledger.log_trade(
                ticker, "SELL", entry_price=basis or None, shares=entry.get("shares"),
                exit_price=fill, pnl_pct=pnl, order_id=sell["id"],
                fill_status="FILLED", filled_avg_price=fill,
                reasoning=f"broker-side exit reconciled: {sell.get('order_type', 'sell')} "
                          f"filled {sell.get('filled_at', '?')}")
            self.ledger.log_position_event(
                ticker, "EXIT", shares=entry.get("shares"),
                reasoning="broker-side exit fill (reconciliation)")
            result.settled += 1
            suffix = f" ({pnl:+.2f}%)" if pnl is not None else ""
            self.notifier.send_alert(
                f"{ticker}: broker-side exit journaled — "
                f"{sell.get('order_type', 'sell')} @ {fill:.2f}{suffix}", "WARN")
        return result

    # ── pass 4: slippage decomposition ─────────────────────────────────────

    def decompose_fills(self) -> PassResult:
        """Split each entry's slippage into the part the strategy chose and the
        part execution cost.

        Measuring a fill against the decision price conflates two different
        things. If the backtest fills at the next session's open, then the move
        from decision price to open is the *strategy's* overnight gap — modeled,
        and sometimes the reason the trade works. Only open → fill is execution
        cost. Gating on the combined number fails by construction and pushes you
        toward "skip the gappers", which in a momentum book means skipping the
        winners.

        Runs at the close because a session's official opening print is not
        reliably available from a delayed feed until well after the open.
        """
        result = PassResult()
        for row in self.ledger.get_fills_needing_decomposition(self.decomposition_days):
            day = row["timestamp"][:10]
            open_px = self.orders.get_daily_open(row["ticker"], day)
            if not open_px:
                result.pending += 1
                continue
            decision = row.get("entry_price") or 0
            fill = row["filled_avg_price"]
            gap = round((open_px - decision) / decision * 100, 3) if decision else None
            execution = round((fill - open_px) / open_px * 100, 3)
            self.ledger.set_fill_decomposition(row["id"], gap, execution)
            result.settled += 1
        return result

    # ── scheduling ─────────────────────────────────────────────────────────

    def run_all(self, decompose: bool = True) -> dict:
        """Run every pass in order, isolating failures.

        One pass raising must not stop the others: they resolve different
        classes of missing row, and the one most likely to fail (a broker
        history query) is not the one whose output matters most. A pass that
        raises is reported and retried on the next run.
        """
        passes = [("entry_fills", self.settle_entry_fills),
                  ("pending_exits", self.settle_pending_exits),
                  ("broker_exits", self.reconcile_broker_exits)]
        if decompose:
            passes.append(("decomposition", self.decompose_fills))

        summary = {}
        for name, fn in passes:
            try:
                summary[name] = fn().as_dict()
            except Exception as e:  # noqa: BLE001 — a pass failing is reportable, not fatal
                summary[name] = {"error": str(e)}
                self.notifier.send_alert(f"Reconciliation pass {name} failed: {e}", "WARN")
        return summary


# ── live-vs-backtest comparison ────────────────────────────────────────────

# EXAMPLE tolerances. Every number below belongs to a specific strategy and
# should come from that strategy's own backtest — these are placeholders that
# only demonstrate the shape of the check.
DEFAULT_TOLERANCES = {
    "win_rate_target_pct": 50.0,
    "win_rate_tolerance_pp": 5.0,
    "min_avg_r": 0.0,
    "max_exec_slippage_pct": 0.3,
    "min_n_for_verdict": 20,
    "min_n_for_slippage": 5,
}


def expectancy_report(ledger, days: int = 30, tolerances: dict | None = None) -> dict:
    """Compare the live record against what the backtest promised.

    Three properties make this useful rather than reassuring:

    **It reports in R.** Percentage returns on a small live book say almost
    nothing; expectancy per unit of risk is directly comparable to a backtest.

    **It refuses to give a verdict on a small sample.** Below the minimum
    trade count the numbers are reported and explicitly marked informational.
    A handful of trades will happily "confirm" any hypothesis you hold.

    **It gates on the median execution cost, not the average.** One bad fill
    on an illiquid open drags an average past any threshold you set, and the
    response to that — tightening entry filters — is usually wrong. The median
    answers the question actually being asked: is execution normally fine?
    """
    tol = {**DEFAULT_TOLERANCES, **(tolerances or {})}
    trips = ledger.get_closed_round_trips(days)
    execution = ledger.get_execution_stats(days)

    pnls = [t["pnl_pct"] for t in trips if t["pnl_pct"] is not None]
    rs = [t["r"] for t in trips if t["r"] is not None]
    exec_slips = execution["exec_slippage_pcts"]
    n = len(pnls)

    report = {
        "window_days": days,
        "closed_trades": n,
        "win_rate_pct": round(len([p for p in pnls if p > 0]) / n * 100, 1) if n else None,
        "avg_pnl_pct": round(sum(pnls) / n, 3) if n else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "r_sample": len(rs),
        "median_exec_slippage_pct": (round(statistics.median(exec_slips), 3)
                                     if exec_slips else None),
        "exec_slippage_n": len(exec_slips),
        "unfilled_orders": execution["unfilled_orders"],
    }

    issues = []
    if n >= tol["min_n_for_verdict"]:
        wr = report["win_rate_pct"]
        if wr is not None and wr < tol["win_rate_target_pct"] - tol["win_rate_tolerance_pp"]:
            issues.append(f"win rate {wr}% below "
                          f"{tol['win_rate_target_pct']}±{tol['win_rate_tolerance_pp']}pp")
        if report["avg_r"] is not None and report["avg_r"] < tol["min_avg_r"]:
            issues.append(f"expectancy {report['avg_r']:+.3f}R below "
                          f"{tol['min_avg_r']:+.3f}R minimum")
    else:
        report["note"] = (f"n={n} < {tol['min_n_for_verdict']} — informational only, "
                          f"no drift verdict")

    med = report["median_exec_slippage_pct"]
    if med is not None and len(exec_slips) >= tol["min_n_for_slippage"] \
            and med > tol["max_exec_slippage_pct"]:
        issues.append(f"median execution slippage {med:+.3f}% over "
                      f"{tol['max_exec_slippage_pct']}% budget")

    report["drift_detected"] = bool(issues)
    report["issues"] = issues
    return report
