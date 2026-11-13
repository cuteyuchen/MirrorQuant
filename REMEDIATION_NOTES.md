# Critical and High audit remediation

This change addresses the Critical/High findings in `CODEX_AUDIT_REPORT.md` through code fixes and conservative execution restrictions. Exchange interactions were tested with offline transports and synthetic accounts; no live trades were placed.

## Trading invariants

- Target notional is `follower_equity * (lead_order_notional / lead_margin_balance) * risk_scale + pending_debt`. Contract quantity uses Decimal division by `price * ct_val` and rounds down to the lot step. Exposure and available-margin caps can reduce an admitted order.
- Cross-margin leverage is calculated separately, targeting initial margin around 8% of equity and bounded by configured, instrument, and current exchange tier limits. The exchange must confirm the resulting leverage before entry.
- Position ownership, weighted entry cost, pending debt, source cumulative fills, and order cumulative fills retain exact decimal strings. Float columns remain for existing API compatibility.
- New entries are restricted to qualified USDT linear perpetuals in supported cross-margin, hedge, single-collateral account modes. Unsupported products, configurations, incomplete filters, or unavailable tier information suspend entries.
- All new entries use price-limited IOC orders. Legacy `market_only` and `ioc_then_market` settings cannot bypass the configured slippage bound. Reductions use reduce-only exchange semantics. An unfilled or unresolved IOC never triggers a blind market replacement.

## Audit coverage

| Findings | Implemented control |
| --- | --- |
| CRIT-01 | Resolve SPA paths and enforce static-root containment, including escaping symlinks. |
| CRIT-02–03 | Persist deterministic order intents before POST; serialize execution across requests/workers; mutation transports never retry; ambiguous submissions reconcile by the original client ID. |
| CRIT-04 | Binding allocation quotas apply to normal reductions, historical reconciliation, orphan closes, and software stops; external inventory is never automatically assigned to a binding. |
| CRIT-05 | Ascending source chronology, completed-cycle admission guards, and cycle timestamps prevent historical closes from consuming reopened allocations. |
| CRIT-06 | Apply only confirmed cumulative fill deltas; zero fills preserve ownership, debt, and protection; uncertain orders remain durable. |
| CRIT-07, HIGH-01 | Stable order identities, cumulative deltas, legacy-key migration, per-binding durable deliveries, individual baselines, and ordered 6,000/1,000 source retention. |
| HIGH-02 | Fresh direct holdings with explicit visibility/provenance gate routing; failed pages and stale/hidden/derived snapshots cannot authorize trading; publication is monotonic across concurrent polls. |
| HIGH-03 | Exact sizing and ledger arithmetic; partial reductions below the minimum are skipped instead of rounded up; confirmed reductions proportionally retire opening debt. |
| HIGH-04–07 | Qualified contract metadata and symbol mappings; actual account-mode/leverage checks; pagination and final order filters; zero available funds stay zero; venue tiers bound leverage. |
| HIGH-08–09 | Invalid books fail closed; IOC prices are rounded inward to the slippage bound; venue depth and conditional-order protocol corrections. |
| HIGH-10 | Durable, binding-scoped software guards use weighted allocation entry cost. Automatic account-wide native stop replacement/cancellation is disabled to avoid modifying another strategy's protection. |
| HIGH-11 | Independent risk monitoring, bounded network/browser operations, continuous loss-breaker evaluation, fresh admission checks, and entry suspension on missing protective quotes. |
| HIGH-12 | Entry filters do not prevent exits; disabled entities retain risk monitoring; serialized lifecycle changes require reconciled flatness, and historical execution ledgers are retained. |
| HIGH-13 | SQLite FULL synchronization; committed pre-submission intents and atomic fill/allocation commits; exchange I/O outside write transactions; rollback recovery. |
| HIGH-14–16 | Strict access-token purpose, issuer/audience, active user and version checks; authenticated cookie/Origin WebSockets with revocation checks; protected MFA lifecycle; session invalidation. |
| HIGH-17–19 | Private, atomic first-admin bootstrap; typed settings validation on writes/import/read; locked key creation, missing-key refusal, and startup credential decryption checks. |
| HIGH-20 | No mutation replay; typed lookup errors; rate-limit cooldowns; clock calibration before signed trading work; conservative suspension when reconciliation is unavailable. |

## Upgrade and operation

Existing SQLite databases receive additive migrations without deleting data. Existing pre-upgrade tokens require a new login. Configure `MQ_BOOTSTRAP_TOKEN` for a new installation and the exact browser origin in `MQ_CORS_ORIGINS`; see `README.md` and `.env.example` for key and authentication requirements.

Existing exchange positions are not retroactively assigned to new allocation rows. They remain external inventory and require deliberate reconciliation. Allocations exceeding physical inventory suspend increases. Accounts/bindings with retained execution history are disabled and retained rather than deleted.

`SUBMITTING`/`UNKNOWN` intents are recovered through exchange lookup, never resubmission. If the exchange cannot establish their outcome, account execution remains suspended and the ledger is retained for investigation. Global execution serialization favors ownership correctness over throughput; SQLite workers must share the same local database and filesystem lock namespace.

Software protection requires a running, connected engine. Loss limits latch entry breakers; they do not silently introduce a new account-flattening policy. Exchange sandbox/live qualification, power-loss testing, and operational latency/capital stress validation remain deployment work. Dynamic initial-margin leverage is not a guarantee against liquidation.

## Verification

Run `backend/.venv/bin/python -m pytest backend/` for the original suite and added regressions. Coverage includes accepted-then-timeout, zero/partial fills, crash-after-fill recovery, allocation quotas on every exit path, legacy cursors, follower outages, stale source races, database upgrades, concurrent bootstrap, JWT substitution, session revocation, and key races. Frontend production compilation and backend bytecode compilation were also checked.
