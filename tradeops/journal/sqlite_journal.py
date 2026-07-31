"""SQLite journal — the system of record for an autonomous agent.

One file, no server, no ORM. It runs on a Raspberry Pi that may lose power
mid-write, gets rsynced to a peer nightly, and is the only artifact that can
answer "what did the agent actually do?" after the fact. That set of
constraints is why this is stdlib `sqlite3` and stays that way.

Three ideas do most of the work here:

**Every row is an event, and event time is not insert time.** Repairs and
backfills are inserted late but stamped with the timestamp of the fill they
describe, so ordering queries key on `timestamp`, never on `id`. The
distinction is not academic: a repaired exit inserted with a higher `id` than
a subsequent re-entry made `MAX(id)` report a live position as closed, which
in turn hid it from the pass that watches for unjournaled broker exits. The
one that was hidden was the position most likely to stop out.

**A fill is a verdict that arrives later than the decision.** Orders are
journaled when placed, with `fill_status` NULL meaning "no verdict yet", and
settled by a later pass that reads the broker (see `reconciliation.py`). A
transient lookup failure must never be recorded as a verdict — rows simply
stay pending and age out.

**Migrations are additive and idempotent.** Columns are added with a guarded
`ALTER TABLE` at startup, because the alternative on a live edge fleet is a
migration tool and a maintenance window for what is usually one nullable
column. New columns are always nullable and consumers treat NULL as "before
this measurement existed".

Scope note: this is the ops half of the production schema. Tables that hold
strategy artifacts (screening output, decision prompts, learned config
history) are deliberately not part of the public extraction.
"""
import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    timestamp TEXT,
    ticker TEXT,
    action TEXT,              -- BUY | SELL | SELL_FAILED
    setup_name TEXT,
    entry_price REAL,         -- price the decision was made at
    stop_loss REAL,
    target REAL,
    shares INTEGER,
    risk_reward REAL,
    reasoning TEXT,
    order_id TEXT,
    exit_price REAL,
    pnl_pct REAL,
    fill_status TEXT,         -- NULL (pending) | FILLED | UNFILLED | PENDING_FILL | FAILED
    filled_avg_price REAL,
    slippage_pct REAL,        -- fill vs decision price (geometry drift monitor)
    gap_pct REAL,             -- decision price -> official open
    exec_slippage_pct REAL    -- official open -> fill (the execution cost)
);
CREATE TABLE IF NOT EXISTS position_events (
    id INTEGER PRIMARY KEY,
    timestamp TEXT,
    ticker TEXT,
    event_type TEXT,
    old_stop REAL,
    new_stop REAL,
    shares INTEGER,
    unrealized_plpc REAL,
    reasoning TEXT
);
CREATE TABLE IF NOT EXISTS equity_snapshots (
    id INTEGER PRIMARY KEY,
    timestamp TEXT,
    date TEXT,
    equity REAL,
    cash REAL,
    buying_power REAL,
    source TEXT               -- 'manual' rows are epoch markers (see below)
);
CREATE TABLE IF NOT EXISTS breaker_events (
    id INTEGER PRIMARY KEY,
    timestamp TEXT,
    event_type TEXT,
    old_level INTEGER,
    new_level INTEGER,
    equity REAL,
    daily_loss_pct REAL,
    drawdown_pct REAL,
    hwm REAL,
    reason TEXT,
    operator TEXT
);
CREATE TABLE IF NOT EXISTS health_checks (
    id INTEGER PRIMARY KEY,
    timestamp TEXT,
    api_ok INTEGER,
    stops_ok INTEGER,
    breaker_level INTEGER,
    entries_blocked INTEGER,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS vulnerability_log (
    id INTEGER PRIMARY KEY,
    timestamp TEXT,
    vulnerability_id TEXT,
    severity TEXT,
    description TEXT,
    auto_remediated INTEGER,
    remediation_action TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ticker_ts ON trades (ticker, timestamp);
CREATE INDEX IF NOT EXISTS idx_equity_date ON equity_snapshots (date);
"""

# Columns log_trade accepts as **fields. An allowlist rather than string
# interpolation of whatever the caller passed: this INSERT is the hottest path
# in the system and the one most likely to be called from a script written in
# a hurry during an incident.
TRADE_FIELDS = (
    "timestamp", "setup_name", "entry_price", "stop_loss", "target", "shares",
    "risk_reward", "reasoning", "order_id", "exit_price", "pnl_pct",
    "fill_status", "filled_avg_price", "slippage_pct", "gap_pct",
    "exec_slippage_pct",
)
EVENT_FIELDS = (
    "timestamp", "old_stop", "new_stop", "shares", "unrealized_plpc", "reasoning",
)
TABLES = ("trades", "position_events", "equity_snapshots", "breaker_events",
          "health_checks", "vulnerability_log")

# Terminal exit states: a SELL row whose action is renamed out of 'SELL' is
# invisible to every consumer that counts realized trades, while still being
# there for an auditor.
TERMINAL_ACTIONS = ("BUY", "SELL", "SELL_FAILED")


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=days)).isoformat()


class SqliteJournal:
    """Implements the `Journal` and `TradeLedger` ports over one SQLite file."""

    def __init__(self, db_path: str, init: bool = True):
        self.db_path = db_path
        if init:
            self.init_db()

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def init_db(self) -> None:
        with self._conn() as con:
            con.executescript(SCHEMA)
        # Additive migrations for databases created by an older build. Each is
        # independently guarded so a partially-migrated file converges instead
        # of failing on the first already-applied statement.
        self._add_columns("trades", {
            "order_id": "TEXT", "exit_price": "REAL", "pnl_pct": "REAL",
            "fill_status": "TEXT", "filled_avg_price": "REAL",
            "slippage_pct": "REAL", "gap_pct": "REAL",
            "exec_slippage_pct": "REAL",
        })

    def _add_columns(self, table: str, columns: dict[str, str]) -> None:
        for name, decl in columns.items():
            try:
                with self._conn() as con:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError:
                pass  # already present

    # ── writes ─────────────────────────────────────────────────────────────

    def log_trade(self, ticker: str, side: str, **fields) -> None:
        """Record a decision and the order placed for it.

        `timestamp` may be passed explicitly, and that is not a convenience: a
        reconstructed row must carry the time of the event it describes, not
        the time someone noticed it was missing. Backdating a repair is what
        keeps a rolling-window query honest — stamping it 'now' silently moves
        an old trade into this week's numbers.
        """
        unknown = set(fields) - set(TRADE_FIELDS)
        if unknown:
            raise ValueError(f"unknown trade fields: {sorted(unknown)}")
        row = {"timestamp": _utcnow(), "ticker": ticker, "action": side}
        row.update(fields)
        cols = ", ".join(row)
        self._insert("trades", cols, row)

    def log_position_event(self, ticker: str, event_type: str, **fields) -> None:
        unknown = set(fields) - set(EVENT_FIELDS)
        if unknown:
            raise ValueError(f"unknown position-event fields: {sorted(unknown)}")
        row = {"timestamp": _utcnow(), "ticker": ticker, "event_type": event_type}
        row.update(fields)
        self._insert("position_events", ", ".join(row), row)

    def _insert(self, table: str, cols: str, row: dict) -> None:
        placeholders = ", ".join("?" * len(row))
        with self._conn() as con:
            con.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
                        tuple(row.values()))

    def log_equity_snapshot(self, equity: float, cash: float | None = None,
                            buying_power: float | None = None,
                            source: str = "auto") -> None:
        now = _utcnow()
        self._insert("equity_snapshots",
                     "timestamp, date, equity, cash, buying_power, source",
                     {"timestamp": now, "date": now[:10], "equity": equity,
                      "cash": cash, "buying_power": buying_power, "source": source})

    def log_breaker_event(self, event: str, old_level: int, new_level: int,
                          equity: float | None = None,
                          daily_loss_pct: float | None = None,
                          drawdown_pct: float | None = None,
                          hwm: float | None = None,
                          reason: str = "", operator: str | None = None) -> None:
        self._insert(
            "breaker_events",
            "timestamp, event_type, old_level, new_level, equity, daily_loss_pct, "
            "drawdown_pct, hwm, reason, operator",
            {"timestamp": _utcnow(), "event_type": event, "old_level": old_level,
             "new_level": new_level, "equity": equity,
             "daily_loss_pct": daily_loss_pct, "drawdown_pct": drawdown_pct,
             "hwm": hwm, "reason": reason, "operator": operator})

    def log_health_check(self, api_ok: bool = True, stops_ok: bool = True,
                         breaker_level: int = 0, entries_blocked: bool = False,
                         notes: str = "") -> None:
        self._insert(
            "health_checks",
            "timestamp, api_ok, stops_ok, breaker_level, entries_blocked, notes",
            {"timestamp": _utcnow(), "api_ok": int(api_ok), "stops_ok": int(stops_ok),
             "breaker_level": breaker_level, "entries_blocked": int(entries_blocked),
             "notes": notes})

    def log_vulnerability(self, vuln_id: str, severity: str, description: str,
                          auto_remediated: bool = False,
                          remediation_action: str = "") -> None:
        """Detection and remediation both leave rows — a go/no-go gate counts
        unremediated criticals, so "fixed it" has to be as auditable as
        "found it"."""
        self._insert(
            "vulnerability_log",
            "timestamp, vulnerability_id, severity, description, auto_remediated, "
            "remediation_action",
            {"timestamp": _utcnow(), "vulnerability_id": vuln_id,
             "severity": severity, "description": description,
             "auto_remediated": int(auto_remediated),
             "remediation_action": remediation_action})

    # ── equity + drawdown baseline ─────────────────────────────────────────

    def get_equity_history(self, days: int = 10) -> list[dict]:
        """Last snapshot per calendar day since the most recent epoch marker,
        newest first."""
        with self._conn() as con:
            epoch = self._epoch_id(con)
            rows = con.execute("""
                SELECT date, equity FROM equity_snapshots
                WHERE id IN (SELECT MAX(id) FROM equity_snapshots
                             WHERE id >= ? GROUP BY date)
                ORDER BY date DESC LIMIT ?
            """, (epoch, days)).fetchall()
        return [{"date": r["date"], "equity": r["equity"]} for r in rows]

    def get_high_water_mark(self) -> float | None:
        """Max equity since the most recent epoch marker.

        A `source='manual'` snapshot is an epoch marker: it says "measure from
        here". Deposits, withdrawals, and account migrations all change equity
        without saying anything about performance, and a drawdown baseline that
        doesn't know about them will either halt on a withdrawal or never halt
        after a deposit.
        """
        with self._conn() as con:
            epoch = self._epoch_id(con)
            row = con.execute("SELECT MAX(equity) AS hwm FROM equity_snapshots "
                              "WHERE id >= ?", (epoch,)).fetchone()
        return row["hwm"] if row and row["hwm"] is not None else None

    @staticmethod
    def _epoch_id(con: sqlite3.Connection) -> int:
        row = con.execute("SELECT MAX(id) AS m FROM equity_snapshots "
                          "WHERE source='manual'").fetchone()
        return (row["m"] if row else None) or 0

    # ── fill settlement (entries) ──────────────────────────────────────────

    def get_pending_buy_fills(self, days: int = 3) -> list[dict]:
        """BUY rows with an order id and no fill verdict yet, recent enough to
        be worth another lookup."""
        return self._select("""
            SELECT id, timestamp, ticker, order_id, entry_price FROM trades
            WHERE action='BUY' AND order_id IS NOT NULL AND fill_status IS NULL
              AND timestamp >= ?
        """, (_cutoff(days),))

    def set_buy_fill(self, trade_id: int, filled_avg_price: float | None = None,
                     slippage_pct: float | None = None,
                     fill_status: str = "FILLED") -> None:
        with self._conn() as con:
            con.execute("""
                UPDATE trades SET filled_avg_price=?, slippage_pct=?, fill_status=?
                WHERE id=?
            """, (filled_avg_price, slippage_pct, fill_status, trade_id))

    # ── fill settlement (exits) ────────────────────────────────────────────

    def get_pending_sell_fills(self, days: int = 3) -> list[dict]:
        """SELL rows journaled PENDING_FILL.

        An exit decided before the open queues a market order that cannot fill
        until the auction. The obvious implementation — place, poll, journal
        the fill — throws before it journals anything, so the exit reasoning
        and the realized loss both disappear. Instead the SELL is journaled
        immediately as PENDING_FILL with its order id, and a later pass settles
        it. Rows that vanish are disproportionately losses; a journal that
        drops them reads better than the account does.
        """
        return self._select("""
            SELECT id, timestamp, ticker, order_id, entry_price, shares FROM trades
            WHERE action='SELL' AND fill_status='PENDING_FILL'
              AND order_id IS NOT NULL AND timestamp >= ?
        """, (_cutoff(days),))

    def settle_sell_fill(self, trade_id: int, exit_price: float,
                         pnl_pct: float | None,
                         filled_avg_price: float | None = None) -> None:
        """Price columns only — never the timestamp. Round-trip pairing joins
        a SELL to the BUY that precedes it in event time; re-stamping a row at
        settlement re-orders history."""
        with self._conn() as con:
            con.execute("""
                UPDATE trades SET exit_price=?, pnl_pct=?, filled_avg_price=?,
                fill_status='FILLED' WHERE id=?
            """, (exit_price, pnl_pct, filled_avg_price or exit_price, trade_id))

    def mark_sell_failed(self, trade_id: int) -> None:
        """The liquidating order died unfilled — the exit did not happen.
        Renaming the action drops the row out of every `action='SELL'` consumer
        while preserving it for audit, which is safer than deleting it and
        safer than leaving a phantom exit in the performance record."""
        with self._conn() as con:
            con.execute("UPDATE trades SET action='SELL_FAILED', fill_status='FAILED' "
                        "WHERE id=?", (trade_id,))

    # ── open-position views ────────────────────────────────────────────────

    def get_open_entry(self, ticker: str) -> dict | None:
        """The live thesis behind a currently-held position: the most recent
        BUY for a ticker, or None if its most recent row is an exit."""
        rows = self._select(
            "SELECT * FROM trades WHERE ticker=? AND action IN ('BUY','SELL','SELL_FAILED') "
            "ORDER BY timestamp DESC LIMIT 1", (ticker,))
        if not rows or rows[0]["action"] != "BUY":
            return None
        return rows[0]

    def get_unclosed_entries(self) -> list[dict]:
        """Filled BUY rows the journal still believes are open.

        Keyed on MAX(timestamp), not MAX(id). A repair row inserted after a
        re-entry but backdated to the real fill sorts *below* that re-entry in
        event time and *above* it by id; keying on id therefore reports the
        live position as closed and hides it from broker-exit reconciliation.
        That is not a hypothetical — it happened to the one position in the
        book that was underwater at the time.

        A trailing SELL_FAILED is excluded deliberately: that path already
        pages for manual intervention (the position may be held unprotected),
        and re-journaling it automatically would paper over it.
        """
        return self._select(f"""
            SELECT t.* FROM trades t
            JOIN (SELECT ticker, MAX(timestamp) AS mts FROM trades
                  WHERE action IN {TERMINAL_ACTIONS}
                  GROUP BY ticker) last
              ON t.ticker = last.ticker AND t.timestamp = last.mts
            WHERE t.action='BUY' AND t.fill_status='FILLED'
        """)

    # ── slippage decomposition ─────────────────────────────────────────────

    def get_fills_needing_decomposition(self, days: int = 5) -> list[dict]:
        return self._select("""
            SELECT id, timestamp, ticker, entry_price, filled_avg_price FROM trades
            WHERE action='BUY' AND fill_status='FILLED'
              AND filled_avg_price IS NOT NULL AND exec_slippage_pct IS NULL
              AND timestamp >= ?
        """, (_cutoff(days),))

    def set_fill_decomposition(self, trade_id: int, gap_pct: float | None,
                               exec_slippage_pct: float) -> None:
        with self._conn() as con:
            con.execute("UPDATE trades SET gap_pct=?, exec_slippage_pct=? WHERE id=?",
                        (gap_pct, exec_slippage_pct, trade_id))

    # ── analysis reads ─────────────────────────────────────────────────────

    def get_recent_trades(self, n: int = 5) -> list[dict]:
        return self._select("SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (n,))

    def get_closed_trades(self, days: int | None = None) -> list[dict]:
        if days is None:
            return self._select("SELECT * FROM trades WHERE action='SELL' "
                                "ORDER BY timestamp")
        return self._select("SELECT * FROM trades WHERE action='SELL' AND timestamp >= ? "
                            "ORDER BY timestamp", (_cutoff(days),))

    def get_closed_round_trips(self, days: int | None = None) -> list[dict]:
        """Each realized exit paired with the entry it closes, expressed in
        R-multiples (P&L as a multiple of the risk the entry took).

        R is the unit that makes a live record comparable to a backtest: it
        normalizes away position size, account size, and the fact that a live
        account trades a handful of names while a backtest trades hundreds.
        Trades whose entry recorded no stop yield no R and are counted in the
        P&L sample only.
        """
        out = []
        with self._conn() as con:
            if days is None:
                sells = con.execute(
                    "SELECT ticker, timestamp, pnl_pct FROM trades "
                    "WHERE action='SELL' AND pnl_pct IS NOT NULL ORDER BY timestamp"
                ).fetchall()
            else:
                sells = con.execute(
                    "SELECT ticker, timestamp, pnl_pct FROM trades "
                    "WHERE action='SELL' AND pnl_pct IS NOT NULL AND timestamp >= ? "
                    "ORDER BY timestamp", (_cutoff(days),)).fetchall()
            for s in sells:
                buy = con.execute("""
                    SELECT entry_price, stop_loss FROM trades
                    WHERE action='BUY' AND ticker=? AND timestamp<?
                    ORDER BY timestamp DESC LIMIT 1
                """, (s["ticker"], s["timestamp"])).fetchone()
                r = None
                if buy and buy["entry_price"] and buy["stop_loss"] \
                        and buy["entry_price"] > buy["stop_loss"]:
                    risk_pct = ((buy["entry_price"] - buy["stop_loss"])
                                / buy["entry_price"] * 100)
                    if risk_pct > 0:
                        r = s["pnl_pct"] / risk_pct
                out.append({"ticker": s["ticker"], "timestamp": s["timestamp"],
                            "pnl_pct": s["pnl_pct"], "r": r})
        return out

    def get_execution_stats(self, days: int = 30) -> dict:
        """Execution-quality sample: the per-fill costs, plus how many orders
        never filled at all (an unfilled limit is not a free pass — it is a
        trade the strategy expected to be in)."""
        rows = self._select("""
            SELECT slippage_pct, gap_pct, exec_slippage_pct FROM trades
            WHERE action='BUY' AND fill_status='FILLED' AND timestamp >= ?
        """, (_cutoff(days),))
        unfilled = self._select("""
            SELECT COUNT(*) AS n FROM trades
            WHERE action='BUY' AND fill_status='UNFILLED' AND timestamp >= ?
        """, (_cutoff(days),))[0]["n"]
        return {
            "exec_slippage_pcts": [r["exec_slippage_pct"] for r in rows
                                   if r["exec_slippage_pct"] is not None],
            "gap_pcts": [r["gap_pct"] for r in rows if r["gap_pct"] is not None],
            "slippage_pcts": [r["slippage_pct"] for r in rows
                              if r["slippage_pct"] is not None],
            "unfilled_orders": unfilled,
        }

    def _select(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._conn() as con:
            return [dict(r) for r in con.execute(sql, params).fetchall()]

    # ── export ─────────────────────────────────────────────────────────────

    def export_table_to_csv(self, table: str, output_path: str) -> int:
        """Dump a table for offline analysis. Table name is checked against a
        constant tuple because it is interpolated into SQL."""
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}; valid: {list(TABLES)}")
        rows = self._select(f"SELECT * FROM {table} ORDER BY id")
        if not rows:
            return 0
        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    def snapshot_counts(self) -> str:
        """Row counts as JSON — cheap enough to log after a backup or a peer
        sync, and the fastest way to notice a database that stopped growing."""
        with self._conn() as con:
            counts = {t: con.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                      for t in TABLES}
        return json.dumps(counts, sort_keys=True)
