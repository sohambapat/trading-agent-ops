"""Journal unit suite — real SQLite files in a tmp directory.

The journal is the one component whose fake would be less trustworthy than the
thing itself, so these run against actual databases. The suite is weighted
toward the properties that make a performance record honest: event-time
ordering, epoch-marked baselines, and rows that are settled by evidence rather
than by assumption.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tradeops.journal import SqliteJournal


def ts(days_ago: float = 0, hour: int = 14) -> str:
    """An ISO timestamp `days_ago` days back, inside every retry window."""
    base = datetime.now(timezone.utc).replace(tzinfo=None, hour=hour, minute=0,
                                              second=0, microsecond=0)
    return (base - timedelta(days=days_ago)).isoformat()


@pytest.fixture()
def journal(tmp_path):
    return SqliteJournal(str(tmp_path / "journal.db"))


# ── schema + migrations ────────────────────────────────────────────────────


def test_init_is_idempotent(tmp_path):
    path = str(tmp_path / "j.db")
    SqliteJournal(path).log_trade("AAA", "BUY", entry_price=10.0)
    again = SqliteJournal(path)
    assert len(again.get_recent_trades()) == 1


def test_migration_adds_columns_to_an_older_database(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE trades (id INTEGER PRIMARY KEY, timestamp TEXT, ticker TEXT,
                             action TEXT, entry_price REAL);
    """)
    con.commit()
    con.close()

    journal = SqliteJournal(path)
    journal.log_trade("AAA", "BUY", entry_price=10.0, exec_slippage_pct=0.11)
    assert journal.get_recent_trades()[0]["exec_slippage_pct"] == 0.11


def test_unknown_trade_field_is_rejected(journal):
    with pytest.raises(ValueError, match="unknown trade fields"):
        journal.log_trade("AAA", "BUY", indicator_value=55)


def test_explicit_timestamp_is_preserved_for_backdated_repairs(journal):
    journal.log_trade("AAA", "SELL", timestamp=ts(9), exit_price=10.0, pnl_pct=-3.0)
    assert journal.get_recent_trades()[0]["timestamp"] == ts(9)


# ── equity baseline ────────────────────────────────────────────────────────


def test_high_water_mark_rebaselines_at_the_latest_manual_marker(journal):
    journal.log_equity_snapshot(100_000, source="manual")
    journal.log_equity_snapshot(120_000, source="auto")
    assert journal.get_high_water_mark() == 120_000

    # a deposit is not performance: the marker moves the baseline with it
    journal.log_equity_snapshot(50_000, source="manual")
    journal.log_equity_snapshot(52_000, source="auto")
    assert journal.get_high_water_mark() == 52_000


def test_equity_history_is_one_row_per_day_newest_first(journal):
    rows = (("2025-12-31", 100_000, "manual"), ("2026-01-01", 100_000, "auto"),
            ("2026-01-02", 99_000, "auto"), ("2026-01-02", 98_500, "auto"),
            ("2026-01-03", 101_000, "auto"))
    with journal._conn() as con:
        for day, equity, source in rows:
            con.execute("INSERT INTO equity_snapshots (timestamp, date, equity, source) "
                        "VALUES (?,?,?,?)", (day + "T21:00:00", day, equity, source))

    history = journal.get_equity_history(days=10)
    assert [h["date"] for h in history] == ["2026-01-03", "2026-01-02", "2026-01-01",
                                            "2025-12-31"]
    assert history[1]["equity"] == 98_500  # last snapshot of the day wins


def test_equity_history_starts_at_the_epoch_marker(journal):
    """Days before the current epoch are not history — they are a different
    capital base."""
    with journal._conn() as con:
        for day, equity, source in (("2026-01-01", 100_000, "auto"),
                                    ("2026-01-02", 50_000, "manual"),
                                    ("2026-01-03", 51_000, "auto")):
            con.execute("INSERT INTO equity_snapshots (timestamp, date, equity, source) "
                        "VALUES (?,?,?,?)", (day + "T21:00:00", day, equity, source))

    assert [h["date"] for h in journal.get_equity_history(days=10)] \
        == ["2026-01-03", "2026-01-02"]


def test_high_water_mark_is_none_before_any_snapshot(journal):
    assert journal.get_high_water_mark() is None


# ── entry fill settlement ──────────────────────────────────────────────────


def test_pending_buy_fills_only_returns_unverdicted_recent_orders(journal):
    journal.log_trade("AAA", "BUY", order_id="o1", entry_price=10.0)
    journal.log_trade("BBB", "BUY", order_id="o2", entry_price=10.0,
                      fill_status="FILLED")
    journal.log_trade("CCC", "BUY", entry_price=10.0)                    # no order id
    journal.log_trade("DDD", "BUY", order_id="o4", timestamp=ts(30))     # aged out
    assert [r["ticker"] for r in journal.get_pending_buy_fills(days=3)] == ["AAA"]


def test_set_buy_fill_records_the_verdict(journal):
    journal.log_trade("AAA", "BUY", order_id="o1", entry_price=10.0)
    trade_id = journal.get_pending_buy_fills()[0]["id"]
    journal.set_buy_fill(trade_id, 10.05, 0.5, "FILLED")
    row = journal.get_recent_trades()[0]
    assert (row["filled_avg_price"], row["slippage_pct"], row["fill_status"]) \
        == (10.05, 0.5, "FILLED")
    assert journal.get_pending_buy_fills() == []


# ── exit fill settlement ───────────────────────────────────────────────────


def test_pending_sell_fills_only_returns_pending_fill_rows(journal):
    journal.log_trade("AAA", "SELL", order_id="s1", entry_price=10.0,
                      fill_status="PENDING_FILL")
    journal.log_trade("BBB", "SELL", order_id="s2", entry_price=10.0,
                      fill_status="FILLED")
    assert [r["ticker"] for r in journal.get_pending_sell_fills()] == ["AAA"]


def test_settling_a_sell_never_moves_its_timestamp(journal):
    """The round-trip join keys on event time; re-stamping at settlement would
    re-order history."""
    journal.log_trade("AAA", "SELL", timestamp=ts(1), order_id="s1",
                      entry_price=10.0, fill_status="PENDING_FILL")
    row = journal.get_pending_sell_fills()[0]
    journal.settle_sell_fill(row["id"], exit_price=9.5, pnl_pct=-5.0)
    settled = journal.get_recent_trades()[0]
    assert settled["timestamp"] == ts(1)
    assert (settled["exit_price"], settled["pnl_pct"], settled["fill_status"]) \
        == (9.5, -5.0, "FILLED")
    assert settled["filled_avg_price"] == 9.5  # defaults to the exit price


def test_failed_exit_leaves_the_performance_record_but_stays_auditable(journal):
    journal.log_trade("AAA", "BUY", timestamp=ts(3), entry_price=10.0,
                      fill_status="FILLED")
    journal.log_trade("AAA", "SELL", timestamp=ts(1), order_id="s1",
                      entry_price=10.0, fill_status="PENDING_FILL")
    sell_id = journal.get_pending_sell_fills()[0]["id"]
    journal.mark_sell_failed(sell_id)

    assert journal.get_closed_trades() == []          # not a realized trade
    assert journal.get_pending_sell_fills() == []     # not still pending
    audit = [t for t in journal.get_recent_trades(10) if t["action"] == "SELL_FAILED"]
    assert len(audit) == 1 and audit[0]["fill_status"] == "FAILED"


# ── open-position views ────────────────────────────────────────────────────


def test_open_entry_is_none_once_the_position_is_closed(journal):
    journal.log_trade("AAA", "BUY", timestamp=ts(5), entry_price=10.0, setup_name="s")
    assert journal.get_open_entry("AAA")["setup_name"] == "s"
    journal.log_trade("AAA", "SELL", timestamp=ts(2), exit_price=11.0)
    assert journal.get_open_entry("AAA") is None
    assert journal.get_open_entry("ZZZ") is None


def test_unclosed_entries_key_on_event_time_not_insert_order(journal):
    """The regression this query exists for.

    A repair row for an old exit is inserted *after* a later re-entry but
    backdated to the real fill. Keyed on MAX(id) the ticker looks closed and
    the live position disappears from reconciliation; keyed on MAX(timestamp)
    it is correctly still open.
    """
    journal.log_trade("AAA", "BUY", timestamp=ts(10), entry_price=10.0,
                      fill_status="FILLED")                       # id 1, oldest
    journal.log_trade("AAA", "BUY", timestamp=ts(5), entry_price=11.0,
                      fill_status="FILLED")                       # id 2, the re-entry
    journal.log_trade("AAA", "SELL", timestamp=ts(7), exit_price=10.5)  # id 3, backdated

    open_entries = journal.get_unclosed_entries()
    assert [(e["ticker"], e["entry_price"]) for e in open_entries] == [("AAA", 11.0)]


def test_unclosed_entries_skip_closed_unfilled_and_failed_exits(journal):
    journal.log_trade("CLOSED", "BUY", timestamp=ts(6), fill_status="FILLED")
    journal.log_trade("CLOSED", "SELL", timestamp=ts(2), exit_price=1.0)
    journal.log_trade("NOFILL", "BUY", timestamp=ts(4), fill_status="UNFILLED")
    journal.log_trade("STUCK", "BUY", timestamp=ts(6), fill_status="FILLED")
    journal.log_trade("STUCK", "SELL_FAILED", timestamp=ts(1), fill_status="FAILED")
    journal.log_trade("HELD", "BUY", timestamp=ts(3), fill_status="FILLED")

    assert [e["ticker"] for e in journal.get_unclosed_entries()] == ["HELD"]


# ── slippage decomposition ─────────────────────────────────────────────────


def test_decomposition_queue_and_write(journal):
    journal.log_trade("AAA", "BUY", entry_price=100.0, filled_avg_price=101.0,
                      fill_status="FILLED")
    journal.log_trade("DONE", "BUY", entry_price=100.0, filled_avg_price=101.0,
                      fill_status="FILLED", exec_slippage_pct=0.2)
    queue = journal.get_fills_needing_decomposition()
    assert [r["ticker"] for r in queue] == ["AAA"]

    journal.set_fill_decomposition(queue[0]["id"], gap_pct=0.8, exec_slippage_pct=0.2)
    assert journal.get_fills_needing_decomposition() == []


# ── analysis reads ─────────────────────────────────────────────────────────


def test_round_trips_pair_each_exit_with_the_entry_it_closes(journal):
    journal.log_trade("AAA", "BUY", timestamp=ts(9), entry_price=100.0,
                      stop_loss=95.0, fill_status="FILLED")     # 5% risk
    journal.log_trade("AAA", "SELL", timestamp=ts(6), exit_price=110.0, pnl_pct=10.0)
    journal.log_trade("BBB", "BUY", timestamp=ts(5), entry_price=50.0,
                      fill_status="FILLED")                      # no stop recorded
    journal.log_trade("BBB", "SELL", timestamp=ts(3), exit_price=48.0, pnl_pct=-4.0)

    trips = journal.get_closed_round_trips(days=30)
    assert [t["ticker"] for t in trips] == ["AAA", "BBB"]
    assert trips[0]["r"] == pytest.approx(2.0)   # +10% on 5% risk
    assert trips[1]["r"] is None                 # no risk denominator, P&L only


def test_round_trip_ignores_an_entry_that_postdates_the_exit(journal):
    journal.log_trade("AAA", "BUY", timestamp=ts(9), entry_price=100.0,
                      stop_loss=90.0, fill_status="FILLED")
    journal.log_trade("AAA", "SELL", timestamp=ts(6), exit_price=110.0, pnl_pct=10.0)
    journal.log_trade("AAA", "BUY", timestamp=ts(2), entry_price=120.0,
                      stop_loss=60.0, fill_status="FILLED")  # re-entry, different risk

    trips = journal.get_closed_round_trips()
    assert len(trips) == 1
    assert trips[0]["r"] == pytest.approx(1.0)  # paired with the 10%-risk entry


def test_execution_stats_report_costs_and_missed_entries(journal):
    journal.log_trade("AAA", "BUY", fill_status="FILLED", slippage_pct=1.2,
                      gap_pct=1.0, exec_slippage_pct=0.2)
    journal.log_trade("BBB", "BUY", fill_status="FILLED", slippage_pct=0.1)
    journal.log_trade("CCC", "BUY", fill_status="UNFILLED")

    stats = journal.get_execution_stats(days=30)
    assert stats["exec_slippage_pcts"] == [0.2]
    assert stats["gap_pcts"] == [1.0]
    assert sorted(stats["slippage_pcts"]) == [0.1, 1.2]
    assert stats["unfilled_orders"] == 1


# ── export ─────────────────────────────────────────────────────────────────


def test_export_writes_csv_and_rejects_unknown_tables(journal, tmp_path):
    journal.log_trade("AAA", "BUY", entry_price=10.0)
    out = tmp_path / "trades.csv"
    assert journal.export_table_to_csv("trades", str(out)) == 1
    assert "AAA" in out.read_text()

    with pytest.raises(ValueError, match="unknown table"):
        journal.export_table_to_csv("trades; DROP TABLE trades", str(out))


def test_export_of_an_empty_table_writes_nothing(journal, tmp_path):
    out = tmp_path / "empty.csv"
    assert journal.export_table_to_csv("trades", str(out)) == 0
    assert not out.exists()


def test_snapshot_counts_covers_every_table(journal):
    journal.log_trade("AAA", "BUY")
    journal.log_health_check(notes="ok")
    journal.log_vulnerability("V-1", "CRITICAL", "missing stop", True, "placed")
    journal.log_breaker_event("TRIP_L1", 0, 1, reason="daily loss")
    journal.log_position_event("AAA", "STOP_RAISED", old_stop=9.0, new_stop=9.5)

    counts = journal.snapshot_counts()
    for table in ("trades", "health_checks", "vulnerability_log", "breaker_events",
                  "position_events", "equity_snapshots"):
        assert f'"{table}"' in counts
    assert '"trades": 1' in counts
