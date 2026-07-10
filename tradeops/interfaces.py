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
        """Open positions: {"ticker", "shares", "avg_cost", "current_price"}."""
        ...

    def get_open_orders(self) -> list[dict]:
        """Open orders: at least {"id", "ticker"}."""
        ...

    def cancel_order(self, order_id: str) -> None: ...

    def close_position(self, ticker: str) -> None:
        """Market-close the full position."""
        ...

    def wait_order_terminal(self, order_id: str) -> None:
        """Block until an order reaches a terminal state (filled/cancelled/
        rejected). Cancels are async at most brokers — a close placed while a
        resting stop still holds the shares is rejected."""
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


class Notifier(Protocol):
    """Human-facing alert channel (e.g. Slack). Severity: INFO/WARN/CRITICAL."""

    def send_alert(self, message: str, severity: str = "INFO") -> None: ...
