# Safety review — IBKR Day Trader

Reviewed on 2026-09-25 against the working tree (uncommitted changes included). The review is read-only: no code changed in `src/` or `run_*.py`. Nothing was connected to IB Gateway or TWS. Every finding below that has a test is reproduced offline in `tests/test_safety.py` using a fake `ib_async.IB`. The fake uses ib_async's real `bracketOrder`, and it clears the portfolio on disconnect the same way `ib_async` does.

Severity scale:
- **Critical**: can send an order you didn't intend, or leave a position with no protection.
- **High**: can bypass a guard or cap, or become dangerous once the loop runs unattended.
- **Medium**: wrong behavior that is usually fail-closed or bounded.
- **Low**: hardening.

---

## 0. The unexplained trade (incident analysis)

`logs/journal.csv` contains exactly one trade:

```
2026-07-13T14:45:49  entry   AAPL BUY 315 @316.84  stop 312.09  tp 332.68  strategy=orb  R:R 3.33
2026-07-13T14:46:19  closed  AAPL  "position no longer held (stop/target/flatten)"
```

What the code says about it:

1. **The "closed" row is 30 s after the entry, which is exactly one poll.** It was written by `_reconcile_closed` (C3). That function forgets a symbol and cancels its whole bracket as soon as the portfolio reads 0 shares. It can't tell apart "rejected", "not filled yet", "filled but the portfolio hasn't updated", and "stopped out". The entry was either rejected (your later TIF fix mentions IBKR error 10349, and the "rejected" branch didn't exist yet), or it simply hadn't filled and got cancelled. Both show up as `closed`.
2. **`strategy=orb` doesn't mean the signal came from ORB.** Both executors journal the global `strategy.name`, whatever strategy actually fired. With your current config, AAPL is assigned `ma_crossover (6/18)` in `live.symbols`, but `run_live.py` ignores per-symbol assignments and trades every symbol with the global strategy, which is ORB (H6). If this came from the CLI loop, AAPL was traded with a strategy you hadn't assigned to it. That's a strong candidate for "unexpected".
3. **The size is larger than intended.** 315 × $316.84 = **US$99,805**. That is 10% of a ~1,000,000 equity reading, but the equity was in **CAD** and the price in **USD** (M1). The real exposure was about 13–14% of the account, not 10%.
4. **Other paths that would look like "the bot traded by itself"** (all reproduced in tests):
   - **C1:** an entry at 09:30 based on *yesterday's* 15:55 bar.
   - **C2:** the loop market-selling shares it never bought. Worse, after a restart it sells a position without cancelling that position's bracket, and the leftover stop or target later sells again. Those child fills happen at IBKR and **are never journaled**.
   - **C4:** repeated market sells in the pre-close window.
   - **H2:** the GUI loop acting on a bar that hasn't closed yet.
   - **H5:** a web page starting the loop through the GUI API.

**To confirm the root cause, check the Gateway/TWS order log for 2026-07-13 14:45–14:47 ET (AAPL, clientId 1).** It will show whether the parent was rejected (10349) or cancelled by the API. Your process logs, if you kept them, will show whether the CLI or GUI loop was running (`LIVE LOOP starting` vs `[LOOP] startup initiated`). The journal alone can't tell them apart: both use client_id 1 and both write the same fields.

---

## 1. Summary

| ID | Sev | Finding | Sync | Async | Test (tests/test_safety.py) |
|---|---|---|---|---|---|
| C1 | Critical | Yesterday's last bar is traded at the open | ✗ | ✗ | `test_c1_previous_session_bar_not_traded_at_open_{sync,async}` |
| C2 | Critical | Exit sells positions the bot doesn't own; untracked bracket legs stay live, so a later fill can go short | ✗ | ✗ | `test_c2_exit_signal_does_not_sell_untracked_position_{sync,async}` |
| C3 | Critical | Reconcile cancels working brackets (and stops) whenever the portfolio reads 0; positions forgotten on disconnect | ✗ | ✗ | `test_c3_unfilled_entry_bracket_not_cancelled_by_reconcile`, `test_c3_disconnect_does_not_forget_open_positions` |
| C4 | Critical | Flatten is fire-and-forget and repeats every poll, so sells stack up and can end short | ✗ | ✗ | `test_c4_flatten_does_not_stack_market_sells` |
| H1 | High | `max_open_positions` ignores pending and same-poll entries | ✗ | ✗ | `test_h1_max_open_positions_counts_same_poll_entries` |
| H2 | High | Async loop treats a bar with ≤60 s left as closed | ✓ | ✗ | `test_h2_forming_bar_final_minute_ignored_async` |
| H3 | High | Kill switch is skipped when P&L is missing; restart/reconnect resets halt and trade count | ✗ | ✗ | `test_h3_no_pnl_data_must_not_allow_entries` |
| H4 | High | No disconnect detection or reconnect; loop runs blind, GUI still says connected; one exception kills the loop | ✗ | ✗ | covered via C3 tests + `test_disconnect_sends_no_orders` |
| H5 | High | GUI endpoints can be triggered cross-site (any web page can start the loop) | – | ✗ | `test_h5_gui_rejects_cross_site_loop_start` |
| H6 | High | CLI loop ignores `live.symbols.<sym>.strategy` | ✗ | ✓ | `test_h6_sync_loop_uses_per_symbol_strategy` |
| H7 | High | Market clock has no holidays or early closes | ✗ | ✗ | `test_h7_market_clock_knows_holidays_and_half_days` |
| M1 | Medium | Sizing mixes CAD equity with USD prices (~1.35–1.4× oversize) | ✗ | ✗ | (no test: needs an FX design decision) |
| M2 | Medium | Zero equity mid-session silently drops the signal | ✗ | ✗ | `test_m2_zero_equity_signal_is_not_silently_dropped` |
| M3 | Medium | Sync `equity_or_raise` uses `time.sleep`, so retries can never succeed | ✗ | ✓ | `test_m3_equity_retry_lets_ib_deliver_account_values` |
| M4 | Medium | Backtester re-enters at the open of the bar it was stopped out in; enters on level, not transition | – | – | `test_m4_backtester_no_reentry_in_the_bar_that_stopped_out` |
| M5 | Medium | `/api/flatten/{symbol}`: no confirmation, sells non-bot shares, sends orders when closed | – | ✗ | – |
| M6 | Medium | `run_trade.py` uses the forming bar and has no market-hours check | ✗ | – | – |
| L1 | Low | `allow_live` isn't type-checked (`"false"` string enables live) | ✗ | ✗ | `test_l1_allow_live_string_false_is_not_treated_as_true` |
| L2 | Low | Position identity by ticker only; shorts count as flat; manual holdings count toward the cap | ✗ | ✗ | – |
| L3 | Low | Misc (dead strategy switch, hanging disconnect, 2-dp prices, weak existing tests) | | | – |

✗ = affected, ✓ = not affected, – = not applicable.

---

## 2. Critical

### C1 — Yesterday's final bar is evaluated (and traded) at the open
- **Where:** `src/live.py:75-86` (`_latest_closed`), `src/live.py:192-240`; `src/async_live.py:54-83`, `src/async_live.py:258-315`.
- **What's wrong:** `_latest_closed` only asks "has this bar's time period ended?". Yesterday's 15:55 bar passes that check, so for the first ~5 minutes of every session the "latest closed bar" is yesterday's. That bar was never evaluated live: from 15:50 the loop sits in the pre-close branch (`live.py:159-165`) and skips `_evaluate`. `last_bar_acted` is in memory only, so it's empty after any restart. The result: a 0→1 flip on yesterday's 15:50→15:55 bars places a bracket at 09:30, with the limit, stop and target all computed from **yesterday's close**.
- **How it triggers:** routinely. Any crossover on the prior session's last bar fires at the next open. After a gap down, the limit fills at the open and the stop can already be at or above the fill, so it's stopped out instantly. After a gap up, the order rests and C3 cancels it 30 s later. It also fires after every restart, and on holidays (H7).
- **Suggested fix:**
  1. Reject a bar whose end is more than about 1× the bar size before now, or that isn't from today's session.
  2. During regular hours, always drop IB's last row (IB includes the bar still in progress). Don't guess from the wall clock.
  3. "Warm start": on startup (and after reconnect), seed `last_bar_acted` with each symbol's current last closed bar, so the first poll never acts on history.

### C2 — The exit path sells positions the bot doesn't own, and leaves their bracket legs live
- **Where:** `src/live.py:205-217`; `src/async_live.py:277-293`; `src/broker.py:176-183`.
- **What's wrong:** `holding` means "the broker shows shares", not "the bot opened this". `_exit_signal` fires whenever the target is 0 (a level, not a transition; that's roughly half of all bars for `ma_crossover`/`vwap`). The loop then market-sells **the entire broker position**. If the symbol isn't in `self.open`, `cancel_orders` is skipped, so the original bracket's take-profit and stop stay live after the market sell. When one of them later triggers, it **sells again and opens a short**. That child fill happens at IBKR and is never journaled.
- **How it triggers:**
  - (a) You hold any watchlist symbol manually in the paper account.
  - (b) You restart `run_live.py`, or click Disconnect/Connect in the GUI, while a bot position is open. `self.open` is in memory and starts empty.
- **Suggested fix:**
  - Only manage what the bot opened. Tag orders with `orderRef`.
  - On startup, rebuild `self.open` from `ib.openTrades()`/`ib.positions()`, adopting positions that have a matching working bracket.
  - Refuse to start (or alert) when a watchlist symbol has an unexplained position.
  - Never sell more than the tracked quantity.

### C3 — Reconcile cancels working brackets whenever the portfolio reads 0
- **Where:** `src/live.py:96-118` (called from 129 and 155); `src/async_live.py:93-116` (called from 127, 166 and 207).
- **What's wrong:** on every poll, any tracked symbol whose portfolio quantity is ≤ 0 is dropped and **all three bracket legs are cancelled**. The quantity is also 0 in three other situations:
  1. **The entry hasn't filled yet.** The limit is at the last close, and the next poll is 30 s later. A pending entry gets cancelled and journaled as `closed`. The trade count isn't refunded, because the status is `Submitted`, not a dead state. This matches the incident journal exactly.
  2. **Fill vs. portfolio-update race.** If the parent filled but `updatePortfolio` hasn't arrived yet, the **stop and take-profit of a live position are cancelled**. The symbol is removed from `self.open`, so the pre-close flatten, kill-switch flatten and strategy exit never touch it again. It's held overnight with no stop.
  3. **Gateway disconnect.** `ib_async`'s `connectionClosed()` calls `wrapper.reset()`, which empties the portfolio. Every tracked position is forgotten and journaled as `closed`. `cancelOrder` raises and the error is swallowed. After reconnect, nothing manages those positions.
- **Suggested fix:**
  - Track the lifecycle from **order state**, not the portfolio. Keep a symbol while its parent is working. Treat it as closed only when a child filled, or the parent is dead with zero fill.
  - Never reconcile while `not ib.isConnected()`.
  - For an entry timeout, cancel **only the parent** (IB cancels the children), wait for the `Cancelled` status, then forget the symbol.

### C4 — Flatten is fire-and-forget and not idempotent, so it can oversell into a short
- **Where:** `src/broker.py:166-183`; `src/async_broker.py:108-114`; `src/live.py:120-129`, `159-173`, `186-189`; `src/async_live.py:118-127`, `210-225`, `251-254`.
- **What's wrong:**
  1. The children are cancelled asynchronously (no wait for confirmation), and a MARKET SELL for the portfolio quantity goes out immediately. If the stop or target fills while the cancel is in flight, both sell, leaving you short.
  2. The pre-close and kill-switch branches call `_flatten_all` on **every poll** while the symbol is in `self.open`. If the sell hasn't filled, or the portfolio hasn't caught up within the 1 s sleep, another full-size market sell goes out each poll. In the test: **4 sells totalling 4,000 shares against a 1,000-share long**, the last one from the shutdown flatten.
  3. Flattening after the close (shutdown at 16:05, or the GUI flatten routes) sends MKT DAY orders that queue for the next open.
  4. A short is never flattened (`if qty > 0`), and reconcile treats ≤ 0 as closed, so a short created by (1) or (2) is silently held.
- **Suggested fix:**
  - Keep a per-symbol "closing" state that holds the flatten order. Don't send another while it's working.
  - Cancel the children and wait for `Cancelled`, or convert the stop child to a market order so IB's OCA handles it atomically.
  - Sell `min(tracked, position)`.
  - Alert and act on any negative position.
  - Don't send market orders when the market is closed.

## 3. High

### H1 — `max_open_positions` ignores pending and same-poll entries
- **Where:** `src/risk.py:97-101`, `110-112`; `src/live.py:156`, `233`; `src/async_live.py:208`, `308`.
- **What's wrong:** `open_positions` is synced once per poll from *filled* portfolio positions, and `note_entry` doesn't increment it. When several symbols signal on the same bar, all of them get in, and working (unfilled) brackets never count.
- **How it triggers:** a market-wide move that flips several symbols at once. The test gets 3 entries with a cap of 2.
- **Suggested fix:** count tracked, working entries as open. Increment in `note_entry`, and have the sync take `max(broker, tracked)`.

### H2 — Async loop evaluates a bar that hasn't closed yet (last 60 s)
- **Where:** `src/async_live.py:73-80` (`still_forming = remaining > 60`).
- **What's wrong:** a bar with ≤ 60 s left counts as closed. With a 30 s poll, about one or two polls per bar land in that window. The signal may disappear by the time the bar closes. Once the loop acts, the real closed bar is skipped because it has the same `bar_ts`. The sync version doesn't do this, and neither does the backtester.
- **Related:** the async loop detects delayed data (`async_live.py:178-192`) but keeps trading. With a delayed feed, the delayed "forming" bar looks closed to a wall-clock check in **both** variants.
- **Suggested fix:** no grace period. Drop the in-progress row (see C1). Halt entries when the data lag exceeds the bar size.

### H3 — Kill switch fails open without P&L; restarts reset risk state
- **Where:** `src/live.py:150-152`; `src/async_live.py:203-205`; `src/broker.py:147-158`; `src/web/state.py:47-64`; `run_live.py:29-38`; `src/risk.py:114-126`.
- **What's wrong:**
  - If `reqPnL` gives NaN (not arrived yet, subscription failed, disconnected), the kill switch isn't checked at all and entries continue.
  - **The first poll after every start always sees None**, because `reqPnL` is subscribed inside that same call and `ib.sleep(0)` doesn't wait.
  - Every restart or GUI reconnect builds a fresh `RiskManager`: `halted=False`, `trades_today=0`, `start_equity` = the already-reduced equity. **After the kill switch trips, a restart allows new entries until P&L arrives, and `max_trades_per_day` starts over.**
  - After a disconnect, the cached `_pnl` object keeps returning a stale value.
- **Suggested fix:**
  - Block entries while P&L is None or stale (track the last update time).
  - Subscribe at connect and wait for the first value.
  - Fall back to NetLiq vs. session-start NetLiq.
  - Persist `halted` and `trades_today` per date, either in a small state file or rebuilt from the journal.

### H4 — Disconnect/reconnect handling (unattended operation)
- **Where:** `src/broker.py`, `src/async_broker.py` (no `disconnectedEvent` handling); `src/live.py:139-190`; `src/async_live.py:194-256`; `src/web/routes_live.py:17-74`; `src/web/state.py`.
- **What's wrong:**
  - Nothing checks `ib.isConnected()`. When the connection drops (IB Gateway's daily restart, network blips), the loop keeps running blind with no reconnect:
    - the portfolio is empty, so C3 forgets every position;
    - equity reads 0;
    - `historical` raises for each symbol;
    - P&L is stale.
  - The GUI keeps reporting `connected: true`.
  - Recovering means a restart or Disconnect/Connect, which triggers H3 (fresh risk state) and C2 (orphaned brackets).
  - Separately, any exception outside the per-symbol `try` ends the loop: the sync version lets it propagate out of `run()`, the async version logs FATAL. Examples are a flatten failing in the pre-close/kill branch, `reqPnL`, or `portfolio()`. The `finally` flatten then runs on the same broken connection.
  - Good news (tested): no order is attempted while disconnected.
- **Suggested fix:**
  - On `ib.disconnectedEvent`, go to "halted: disconnected" (no reconcile, no entries).
  - Reconnect with backoff, **re-running the DU/allow_live guard**.
  - Rebuild tracked state from `openTrades()`/`positions()` before resuming.
  - Expose `ib.isConnected()` in `/api/status`.
  - Wrap each loop phase in its own `try`.

### H5 — The GUI API can be driven from any web page (CSRF / DNS rebinding)
- **Where:** `src/web/routes_live.py:114-123` (`/api/connect`), `135-145` (`/api/loop/start`), `181-187` (`/api/flatten/{symbol}`); `src/web/app.py`.
- **What's wrong:**
  - Binding to 127.0.0.1 doesn't stop the browser from making requests. The endpoints have no auth and no Origin/Host check.
  - A body-less POST is a "simple" request. Any page open in your browser can run `fetch("http://127.0.0.1:8000/api/loop/start", {method: "POST", mode: "no-cors"})`, and the browser sends it with no preflight. The response is opaque, but the side effect happens.
  - `/api/connect` followed by `/api/loop/start` means auto-trading started by a third-party page. The test gets HTTP 200 and the loop starts.
  - `flatten-all` and `watchlist` are only protected by accident, because they need a JSON body.
- **Suggested fix:**
  - Add middleware that rejects non-GET requests unless `Origin` (or `Referer`) is `http://127.0.0.1:<port>`/`localhost`, and `Host` is local.
  - Require a per-launch token or custom header that `app.js` sends.
  - Require `{"confirm": true}` for loop start and single-symbol flatten.

### H6 — The CLI loop ignores per-symbol strategies
- **Where:** `src/live.py:66-67`, `202`; also the journal field at `src/executor.py:49` and `src/async_executor.py:39`.
- **What's wrong:**
  - `LiveTrader` builds one strategy from `strategy.name` for all symbols, while the GUI loop uses `live.symbols.<sym>.strategy`.
  - With your config, `run_live.py` trades AAPL with **ORB**, even though AAPL is assigned **ma_crossover 6/18**. `run_live.py`'s startup banner prints the per-symbol map, which suggests otherwise.
  - Both executors journal the global name, so the journal can't show which strategy actually fired.
- **Suggested fix:** build the per-symbol map in one shared helper used by both traders, and pass the strategy name into `enter_long` for the journal.

### H7 — The market clock doesn't know holidays or early closes
- **Where:** `src/broker.py:185-203`.
- **What's wrong:**
  - The clock only knows Mon–Fri 09:30–16:00.
  - **Early closes (2026-11-27 and 2026-12-24 close at 13:00):**
    - the loop thinks 3 hours remain, so positions stay open past the real close;
    - the DAY stop and target legs expire at 13:00;
    - the 15:50 flatten sends market orders that queue for the next session;
    - result: positions carried overnight without protection, sold at the next open.
  - **Holidays (e.g. 2026-11-26):** the day counts as open, so the loop evaluates stale bars (C1) and can submit brackets that wait for the next session.
  - If `zoneinfo` fails, the clock silently falls back to machine local time.
- **Suggested fix:** use an exchange calendar (e.g. `exchange_calendars`, which works offline) or IB `reqContractDetails` trading/liquid hours cached per day. If the calendar isn't available, fail closed.

## 4. Medium

### M1 — Position sizing mixes CAD equity with USD prices
- **Where:** `src/broker.py:80-96`; `src/live.py:227-229`; `src/async_live.py:302-304`; `src/risk.py:19-32`; `run_trade.py:66-68`.
- **What's wrong:**
  - With `currency: CAD`, equity is CAD NetLiquidation. `max_notional = CAD × pct`, but shares are computed as `max_notional / USD price`. Real exposure is therefore about 1.35–1.4× `max_position_pct`, as in the incident (US$99.8k ≈ 10% of a CAD number).
  - The kill switch is consistent: both the level and dailyPnL are in base currency.
  - If `account.currency` is misconfigured, `equity()` silently uses the first non-zero NetLiquidation in any currency. The only signal is a `log.warning`, and no logging is configured.
- **Suggested fix:** size in the contract's currency using the `ExchangeRate` account value (or a qualified FX quote), and fail closed if the rate is missing. Treat a configured currency that isn't found as an error, not a fallback.

### M2 — Zero or missing equity mid-session is silent
- **Where:** `src/broker.py:96`; `src/live.py:227-232`; `src/async_live.py:302-307`.
- **What's wrong:** `equity()` returns `0.0` for "unknown" (for example after a disconnect). That gives 0 shares, and the bar is marked as acted with **no journal entry or log**. It is fail-closed for orders (tested: no order goes out), but the bot is blind without telling you. Startup is correctly protected by `equity_or_raise` (tested, including the CAD regression).
- **Suggested fix:** return `None` for "unknown", journal `blocked: equity unavailable`, and don't consume the bar.

### M3 — Sync `equity_or_raise` blocks ib_async's event loop
- **Where:** `src/broker.py:112`.
- **What's wrong:** `time.sleep` stops ib_async from processing incoming messages, so retries can never see account values that didn't arrive during `connect()`'s 4 s. It fails closed (SafetyError after about 20 s) but makes startup flaky, and it plausibly contributed to the earlier "equity reads zero" symptoms. The async version is correct.
- **Suggested fix:** use `self.ib.sleep(delay)`.

### M4 — Backtester fills that live trading can't get
- **Where:** `src/backtester.py:82-118`.
- **What's wrong:**
  1. After a stop or target exit inside bar *i*, it re-enters at bar *i*'s **open**, a price that traded *before* the exit. That's a time-travel fill.
  2. It enters whenever target == 1 and it's flat (a level), while live requires a fresh 0→1 transition. So the backtest re-enters after every stop or target and trades much more than live would.
  3. Stop and target aren't checked on the entry bar.
- **What holds up (tested):** next-bar-open execution, and no look-ahead. Changing future bars doesn't change past equity.
- **Suggested fix:** don't enter on a bar that had an intrabar exit; use the same entry rule as live; check stop and target starting from the entry bar.

### M5 — Single-symbol flatten from the GUI
- **Where:** `src/web/routes_live.py:181-187`; `src/async_live.py:155-166`; `src/web/static/app.js:331-332`.
- **What's wrong:** it sells the **whole broker position** (not the bot's quantity), with no `confirm` flag (flatten-all has one). It's reachable cross-site (H5), and it sends MKT orders even when the market is closed.
- **Suggested fix:** require confirmation, sell only the tracked quantity unless forced, and refuse when the market is closed.

### M6 — `run_trade.py` evaluates the forming bar at any time of day
- **Where:** `run_trade.py:48-60`, `85-91`.
- **What's wrong:** there's no closed-bar filter and no market-hours check, and it uses the global strategy. Outside regular hours a DAY bracket gets queued for the next open at a stale price. The `yes` prompt mitigates this, but `--yes` removes it.
- **Suggested fix:** reuse the loop's closed-bar and market-hours checks, and disallow `--yes` in live mode.

## 5. Low

- **L1 — `allow_live` truthiness** (`src/config.py:100-111`, `src/broker.py:38`, `src/async_broker.py:25`). A quoted `allow_live: "false"` (or `"no"`) is a truthy string and unlocks live mode. Fix: validate `isinstance(allow_live, bool)` and compare `is True`.
- **L2 — Position identity.**
  - `src/broker.py:160-164` matches on `contract.symbol` only, so an option or a different listing with the same ticker counts as the stock.
  - A short (quantity < 0) counts as "not holding", so the loop can buy on top of a short.
  - `len(portfolio())` counts manual and non-stock holdings toward `max_open_positions`.
  - Fix: match on the qualified contract's `conId`, and count only bot-managed positions.
- **L3 — Miscellaneous.**
  - `routes_config.update_strategy` sets `trader.strat`, which the async trader never reads, so the switch silently does nothing (`src/web/routes_config.py:108-109`).
  - `do_disconnect` awaits the loop task without cancelling it, so the HTTP call can hang up to 120 s (`src/web/state.py:69-75`).
  - `bracket_prices` rounds to 2 decimals, which gives invalid ticks for stocks under $1 (`src/risk.py:35-42`).
  - The sync `_latest_closed` compares naive bar times to the machine's local clock.
  - `tests/test_web.py` #3 and #5 never produce an entry (their bars end at target 1→1), so they don't exercise the entry path they describe.

## 6. Paper/live guard — verdict

**No bypass found.** Specifically:
- `Broker.connect` and `AsyncBroker.connect` both refuse `mode: live` without `allow_live` **before** creating an IB connection.
- After connecting, both check the account prefix: DU for paper, non-DU for live. On a mismatch they disconnect and raise.
- `broker.ib` is assigned only after those checks pass. It's the only assignment anywhere in `src/` or `run_*.py`.
- Every order path goes through `broker.ib`: both executors, and flatten through both brokers. So no order can precede the check.
- `/api/connect` uses the same `AsyncBroker.connect`.
- `save_config` never writes the `broker` section.
- All of this is tested with the fake IB, including the web `/api/connect` path.

Caveats:
- L1 (string truthiness).
- The check runs once, at connect. That's fine today because there's no auto-reconnect, but any reconnect added for H4 **must** re-run it.
- `--yes` on `run_live.py`/`run_trade.py` skips the human confirmation by design.
- The GUI has no confirmation for starting the loop at all, and H5 makes that reachable cross-site.

## 7. Not covered / limits of this review

- No real IB behavior was observed. Portfolio-update latency, order status sequences, error 10349 and the delayed-data format are inferred from the `ib_async` 2.x source in `.venv` and IB's documented behavior. The fake IB reproduces ib_async's reset-on-disconnect and uses its real `bracketOrder`, but it doesn't simulate fills of bracket children.
- Not reviewed in depth: the frontend beyond its order-affecting calls, `screener.py`/`data.py` (yfinance), the indicator math (spot-checked only), and the terminal dashboard (confirmed read-only).
- The CSRF test drives the app over raw ASGI because `httpx` isn't installed, so there's no TestClient.
- M1 has no test: the correct behavior depends on how you want FX handled.

## 8. Test results

Baseline before any changes: `tests/test_core.py` passed; `tests/test_web.py` passed (22 checks).

After adding `tests/test_safety.py` (54 tests):

```
python tests/test_core.py    -> ALL CORE TESTS PASSED
python tests/test_web.py     -> ALL WEB VERIFICATION TESTS PASSED
python tests/test_safety.py  -> 37 passed, 17 failed on known bugs (REVIEW.md), 0 unexpected failures
```

The 17 failing tests are all tagged `@known_bug("<ID>")` and map to the table in §1: C1×2, C2×2, C3×2, C4, H1, H2, H3, H5, H6, H7, M2, M3, M4, L1. They're meant to fail until each fix lands. When one starts passing, the runner prints `bug <ID> FIXED?` so the marker can be removed.
