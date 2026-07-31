# Porting roadmap

This repo is an incremental, sanitized extraction of a private production
system. Modules land one tranche at a time; each tranche ships with its tests
and docs.

## Porting rules (applied to every tranche)

1. **Secrets scan before commit** — no keys, webhooks, account identifiers,
   hostnames, or IPs.
2. **Parameters are genericized** — thresholds and sizing in this repo are
   illustrative defaults, not production values.
3. **No learned artifacts** — strategy files, prompt configs, and journals
   written by the agents themselves never leave the private system.
4. **Interfaces over SDK imports** — modules are ported against the ports in
   `tradeops/interfaces.py` so each tranche stands alone and stays testable.

## Tranches

| # | Tranche | Contents | Status |
|---|---------|----------|--------|
| 1 | Safety | Circuit breaker (L1 soft halt / L2 hard halt / L3 flatten / fail-closed), halt-state persistence, self-drill, manual reset | ✅ ported |
| 2 | Alerting | Severity-tiered Slack/SMTP/console notifier chain (`FallbackNotifier`), daily health check (LLM probe, broker reachability, stop-coverage remediation, circuit breaker, composable extra probes) | ✅ ported |
| 3 | Journal | SQLite trade/equity/event journal (event-time ordering, epoch-marked equity baseline, additive migrations), fill settlement for entries and pre-open exits, broker-side exit reconciliation, slippage decomposition, live-vs-backtest expectancy report | ✅ ported |
| 4 | Broker adapter | Alpaca implementation of the `Broker` port, order-lifecycle helpers (terminal-state waits, OTO handling, EOD sweep) | planned |
| 5 | Backup | Tarball-bundled cloud backup with timeout hardening and retry | planned |
| 6 | Deploy | md5-verified deploy scripts, systemd units, agent-state exclude lists | planned |
| 7 | Ops docs | Runbook excerpts, incident postmortems, go/no-go gate structure | planned |
| 8 | Learning-loop ops | Design notes on the bounded self-improvement loop: experiment registry, auto-revert, audit tiers (concepts only — no strategy content) | planned |
