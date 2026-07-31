"""Ports for the external dependencies of the ops layer.

The safety layer is the last line of defense, so it must stay import-clean of
any concrete broker SDK or storage backend: a broken exchange-SDK upgrade
should never be able to take the circuit breaker down with it. Concrete
adapters (broker, SQLite journal, Slack notifier) are injected at composition
time; reference implementations land in this repo tranche by tranche
(see docs/ROADMAP.md).
"""
from typing import Protocol


class Broker(Protocol):
    """Minimal broker surface the safety layer needs."""

    def get_portfolio(self) -> dict:
        """Account snapshot: at least {"total_value": float, "cash": float,
        "buying_power": float}. Raises on connectivity failure."""
        ...

    def get_positions(self) -> list[dict]:
        """Open positions: {"ticker", "shares", "avg_cost", "current_price"};
        may include "qty_available" (shares not reserved by resting orders)."""
        ...

    def get_open_orders(self) -> list[dict]:
        """Open orders: at least {"id", "ticker", "type"}."""
        ...

    def cancel_order(self, order_id: str) -> None: ...

    def close_position(self, ticker: str) -> None:
        """Market-close the full position."""
        ...

    def place_stop_order(self, ticker: str, shares: int, stop_price: float) -> None:
        """Rest a protective sell-stop. Raises on rejection."""
        ...

    def wait_order_terminal(self, order_id: str) -> None:
        """Block until an order reaches a terminal state (filled/cancelled/
        rejected). Cancels are async at most brokers — a close placed while a
        resting stop still holds the shares is rejected."""
        ...


class OrderHistory(Protocol):
    """Post-trade lookups. Separate from `Broker` because this is the
    read-only, after-the-fact surface reconciliation needs — a component that
    only settles yesterday's records should not be handed a port that can
    place orders."""

    def get_order(self, order_id: str) -> dict | None:
        """{"id", "status", "filled_avg_price"} or None if the lookup failed
        (a transient error must be distinguishable from a real verdict —
        returning None means "ask again later", never "it didn't fill")."""
        ...

    def get_recent_filled_sell(self, ticker: str, since_iso: str) -> dict | None:
        """Most recent FILLED sell-side order for a ticker submitted after
        `since_iso`: {"id", "filled_avg_price", "filled_at", "order_type"}.
        This is how an autonomous stop fill is found after the fact."""
        ...

    def get_daily_open(self, ticker: str, day: str) -> float | None:
        """Official opening print for a session (YYYY-MM-DD), or None if the
        bar isn't available yet."""
        ...


class Journal(Protocol):
    """Append-only audit log; every safety action must leave a row."""

    def log_equity_snapshot(self, equity: float, cash: float | None,
                            buying_power: float | None, source: str) -> None: ...

    def get_equity_history(self, days: int) -> list[dict]:
        """Daily equity rows, NEWEST FIRST: {"date": "YYYY-MM-DD", "equity": float}."""
        ...

    def get_high_water_mark(self) -> float | None: ...

    def log_breaker_event(self, event: str, old_level: int, new_level: int,
                          equity: float | None = None,
                          daily_loss_pct: float | None = None,
                          drawdown_pct: float | None = None,
                          hwm: float | None = None,
                          reason: str = "", operator: str | None = None) -> None: ...

    def log_trade(self, ticker: str, side: str, **fields) -> None: ...

    def log_health_check(self, **fields) -> None:
        """One row per scheduled health check (probe results, notes)."""
        ...

    def log_vulnerability(self, vuln_id: str, severity: str, description: str,
                          auto_remediated: bool, remediation_action: str) -> None:
        """Detection AND remediation both leave rows — the go/no-go gate
        counts unremediated criticals."""
        ...


class TradeLedger(Protocol):
    """The settlement surface: the subset of journal reads/writes that the
    reconciliation pass needs. `SqliteJournal` implements both this and
    `Journal`; they are split so the reconciler can be handed (and faked) as
    exactly what it uses.

    Every "get_*" here returns rows still awaiting a verdict, and every "set_*"
    records one. Orders are keyed on EVENT TIME, not insert order — see
    `SqliteJournal.get_unclosed_entries`.
    """

    def get_pending_buy_fills(self, days: int) -> list[dict]: ...
    def set_buy_fill(self, trade_id: int, filled_avg_price: float | None = None,
                     slippage_pct: float | None = None,
                     fill_status: str = "FILLED") -> None: ...

    def get_pending_sell_fills(self, days: int) -> list[dict]: ...
    def settle_sell_fill(self, trade_id: int, exit_price: float,
                         pnl_pct: float | None,
                         filled_avg_price: float | None = None) -> None: ...
    def mark_sell_failed(self, trade_id: int) -> None: ...

    def get_unclosed_entries(self) -> list[dict]: ...

    def get_fills_needing_decomposition(self, days: int) -> list[dict]: ...
    def set_fill_decomposition(self, trade_id: int, gap_pct: float | None,
                               exec_slippage_pct: float) -> None: ...

    def log_trade(self, ticker: str, side: str, **fields) -> None: ...
    def log_position_event(self, ticker: str, event_type: str, **fields) -> None: ...


class Notifier(Protocol):
    """Human-facing alert channel (e.g. Slack). Severity: INFO/WARN/CRITICAL."""

    def send_alert(self, message: str, severity: str = "INFO") -> None: ...
