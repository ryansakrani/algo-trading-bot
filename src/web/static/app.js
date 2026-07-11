/* IBKR Day Trader — client-side logic */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

/* ---- State ---- */
let state = {};
let evtSource = null;
let localSymbols = {};
let symDirty = false;
const STRAT_PARAMS = {
  orb: ["opening_minutes"],
  ma_crossover: ["fast", "slow"],
  mean_reversion: ["rsi_period", "oversold", "overbought"],
  vwap: ["vwap_band_pct"],
};

/* ---- SSE ---- */
function connectSSE() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource("/api/stream");
  evtSource.onmessage = (e) => {
    state = JSON.parse(e.data);
    render(state);
  };
  evtSource.onerror = () => {
    setTimeout(connectSSE, 3000);
  };
}

/* ---- Render ---- */
function render(s) {
  const badge = $("#mode-badge");
  badge.textContent = s.mode || "---";
  badge.className = s.mode === "live" ? "live" : "paper";

  const dot = $("#conn-dot");
  dot.className = s.connected ? "on" : "";
  $("#conn-label").textContent = s.connected
    ? `Connected: ${s.account || ""}`
    : "Disconnected";

  setText("#hdr-equity", s.connected ? fmt$(s.equity) : "---");

  const pnlEl = $("#hdr-pnl");
  if (s.connected) {
    pnlEl.textContent = fmt$(s.daily_pnl);
    pnlEl.className = "val " + (s.daily_pnl >= 0 ? "pnl-pos" : "pnl-neg");
  } else {
    pnlEl.textContent = "---";
    pnlEl.className = "val";
  }

  const kb = $("#kill-badge");
  if (s.kill_switch_active) {
    kb.textContent = "HALTED";
    kb.className = "halt";
  } else {
    kb.textContent = s.connected ? `Kill @ ${fmt$(s.kill_switch_level)}` : "---";
    kb.className = "ok";
  }

  const lb = $("#loop-badge");
  lb.textContent = s.loop_status || "stopped";
  lb.className = s.loop_status || "stopped";

  setText("#hdr-market", s.market_open ? `Open (${s.minutes_to_close}m left)` : "Closed");

  setText("#hdr-trades", s.connected
    ? `${s.trades_today}/${s.max_trades_per_day}` : "---");
  setText("#hdr-open", s.connected
    ? `${s.open_positions_count}/${s.max_open_positions}` : "---");

  $("#btn-connect").disabled = s.connected;
  $("#btn-disconnect").disabled = !s.connected;
  $("#btn-start").disabled = !s.connected || s.loop_status === "running";
  $("#btn-stop").disabled = !s.connected || s.loop_status === "stopped";
  $("#btn-flatten-all").disabled = !s.connected;

  renderPositions(s.open_positions || []);

  if (s.symbols && !symDirty) {
    localSymbols = s.symbols;
    renderSymTable(localSymbols);
  }
}

function renderSymTable(syms) {
  const tbody = $("#sym-body");
  const entries = Object.entries(syms);
  if (!entries.length) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-dim)">No symbols</td></tr>';
    return;
  }
  tbody.innerHTML = entries.map(([sym, cfg]) => {
    const stName = cfg.strategy || "---";
    const params = cfg.params || {};
    const paramKeys = STRAT_PARAMS[stName] || [];
    const paramStr = paramKeys.map(k => {
      const v = params[k] != null ? params[k] : "";
      return `<span style="margin-right:6px">${k}=<input type="text" value="${v}" style="width:40px" data-sym="${sym}" data-param="${k}" onchange="onParamEdit(this)"></span>`;
    }).join("");
    const stratOpts = Object.keys(STRAT_PARAMS).map(s =>
      `<option value="${s}" ${s === stName ? "selected" : ""}>${s}</option>`
    ).join("");
    return `<tr>
      <td><strong>${sym}</strong></td>
      <td><select data-sym="${sym}" onchange="onStratChange(this)">${stratOpts}</select></td>
      <td style="text-align:left">${paramStr}</td>
      <td><button class="btn btn-red" onclick="removeSym('${sym}')">x</button></td>
    </tr>`;
  }).join("");
}

function onStratChange(sel) {
  symDirty = true;
  const sym = sel.dataset.sym;
  const newStrat = sel.value;
  localSymbols[sym].strategy = newStrat;
  const defaults = { orb: {opening_minutes:30}, ma_crossover: {fast:9,slow:21},
    mean_reversion: {rsi_period:14,oversold:30,overbought:70}, vwap: {vwap_band_pct:0} };
  localSymbols[sym].params = {...(defaults[newStrat] || {})};
  renderSymTable(localSymbols);
}

function onParamEdit(inp) {
  symDirty = true;
  const sym = inp.dataset.sym;
  const param = inp.dataset.param;
  let val = inp.value;
  val = isNaN(Number(val)) ? val : Number(val);
  if (localSymbols[sym]) localSymbols[sym].params[param] = val;
}

function removeSym(sym) {
  symDirty = true;
  delete localSymbols[sym];
  renderSymTable(localSymbols);
}

function addSymbol() {
  symDirty = true;
  const inp = $("#add-sym-input");
  const sym = inp.value.trim().toUpperCase();
  if (!sym) return;
  const strat = $("#add-sym-strat").value;
  const defaults = { orb: {opening_minutes:30}, ma_crossover: {fast:9,slow:21},
    mean_reversion: {rsi_period:14,oversold:30,overbought:70}, vwap: {vwap_band_pct:0} };
  localSymbols[sym] = { strategy: strat, params: {...(defaults[strat] || {})} };
  inp.value = "";
  renderSymTable(localSymbols);
}

function renderPositions(positions) {
  const tbody = $("#positions-body");
  if (!positions.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-dim)">No open positions</td></tr>';
    return;
  }
  tbody.innerHTML = positions.map(p => {
    const pnlCls = p.unrealized_pnl >= 0 ? "pnl-pos" : "pnl-neg";
    return `<tr>
      <td>${p.symbol}</td>
      <td>${p.shares}</td>
      <td>${p.entry_price?.toFixed(2) || p.avg_cost?.toFixed(2) || "---"}</td>
      <td>${p.market_price?.toFixed(2) || "---"}</td>
      <td class="${pnlCls}">${fmt$(p.unrealized_pnl)}</td>
      <td><button class="btn btn-red" onclick="flattenOne('${p.symbol}')">Close</button></td>
    </tr>`;
  }).join("");
}

/* ---- Journal ---- */
async function loadJournal() {
  try {
    const resp = await fetch("/api/journal?n=15");
    const rows = await resp.json();
    const tbody = $("#journal-body");
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-dim)">No entries</td></tr>';
      return;
    }
    tbody.innerHTML = rows.reverse().map(r =>
      `<tr><td>${(r.ts || "").substring(11)}</td><td>${r.event}</td>` +
      `<td>${r.symbol}</td><td>${r.side} ${r.qty}</td>` +
      `<td>${(r.note || "").substring(0, 40)}</td></tr>`
    ).join("");
  } catch (_) {}
}
setInterval(loadJournal, 5000);

/* ---- API calls ---- */
async function api(method, path, body) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || JSON.stringify(err));
  }
  return r.json();
}

async function doConnect()    { try { await api("POST", "/api/connect"); } catch(e) { alert(e.message); } }
async function doDisconnect() { try { await api("POST", "/api/disconnect"); } catch(e) { alert(e.message); } }
async function doStart()      { try { await api("POST", "/api/loop/start"); } catch(e) { alert(e.message); } }
async function doStop()       { try { await api("POST", "/api/loop/stop"); } catch(e) { alert(e.message); } }

function showFlattenModal() { $("#flatten-modal").classList.add("open"); }
function hideFlattenModal() { $("#flatten-modal").classList.remove("open"); }

async function confirmFlatten() {
  hideFlattenModal();
  try { await api("POST", "/api/flatten-all", { confirm: true }); }
  catch(e) { alert(e.message); }
}

async function flattenOne(sym) {
  try { await api("POST", `/api/flatten/${sym}`); }
  catch(e) { alert(e.message); }
}

async function saveWatchlist() {
  try {
    await api("POST", "/api/watchlist", { symbols: localSymbols });
    symDirty = false;
  } catch(e) { alert(e.message); }
}

async function saveRisk() {
  const fields = ["max_position_pct", "per_trade_stop_pct", "take_profit_pct",
                   "daily_max_loss_pct", "max_open_positions", "max_trades_per_day"];
  const body = {};
  for (const f of fields) {
    const el = $(`#risk-${f}`);
    if (el && el.value !== "") {
      body[f] = f.includes("max_open") || f.includes("max_trades")
        ? parseInt(el.value) : parseFloat(el.value);
    }
  }
  try {
    const result = await api("PUT", "/api/config/risk", body);
    populateRisk(result);
  } catch(e) { alert(e.message); }
}

async function loadRisk() {
  try {
    const r = await api("GET", "/api/config/risk");
    populateRisk(r);
  } catch(_) {}
}

function populateRisk(r) {
  for (const [k, v] of Object.entries(r)) {
    const el = $(`#risk-${k}`);
    if (el) el.value = v;
  }
}

/* ---- Backtest ---- */
async function runBacktest() {
  const btn = $("#btn-backtest");
  btn.disabled = true;
  btn.textContent = "Running...";
  try {
    const body = {
      symbol: $("#bt-symbol").value || "AAPL",
      strategy: $("#bt-strategy").value || "ma_crossover",
      period: $("#bt-period").value || "60d",
      interval: $("#bt-interval").value || "5m",
    };
    const r = await api("POST", "/api/backtest", body);
    renderBtStats(r.stats);
    renderEquityCurve(r.equity_curve);
    renderBtTrades(r.trades);
    $("#bt-results").style.display = "block";
  } catch(e) {
    alert(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Backtest";
  }
}

function renderBtStats(stats) {
  const items = [
    ["Trades", stats.trades],
    ["Return", stats.total_return_pct + "%"],
    ["Win Rate", stats.win_rate + "%"],
    ["Profit Factor", stats.profit_factor],
    ["Max DD", stats.max_drawdown_pct + "%"],
    ["Sharpe", stats.sharpe],
    ["Avg Win", fmt$(stats.avg_win)],
    ["Avg Loss", fmt$(stats.avg_loss)],
  ];
  $("#bt-stats").innerHTML = items.map(([l, v]) =>
    `<div class="stat-card"><div class="val">${v}</div><div class="lbl">${l}</div></div>`
  ).join("");
}

function renderEquityCurve(curve) {
  const canvas = $("#eq-chart");
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  const pad = { t: 10, r: 10, b: 20, l: 60 };

  const vals = curve.map(p => p.equity);
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const range = mx - mn || 1;

  ctx.clearRect(0, 0, W, H);

  ctx.strokeStyle = "#3a1818";
  ctx.lineWidth = 0.5;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (H - pad.t - pad.b) * (1 - i / 4);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.fillStyle = "#997777";
    ctx.font = "10px monospace";
    ctx.textAlign = "right";
    ctx.fillText(fmt$(mn + range * i / 4), pad.l - 4, y + 3);
  }

  ctx.beginPath();
  ctx.strokeStyle = "#cc4444";
  ctx.lineWidth = 1.5;
  const n = vals.length;
  for (let i = 0; i < n; i++) {
    const x = pad.l + (W - pad.l - pad.r) * (i / (n - 1 || 1));
    const y = pad.t + (H - pad.t - pad.b) * (1 - (vals[i] - mn) / range);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();

  const startY = pad.t + (H - pad.t - pad.b) * (1 - (vals[0] - mn) / range);
  ctx.strokeStyle = "#555";
  ctx.lineWidth = 0.5;
  ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(pad.l, startY); ctx.lineTo(W - pad.r, startY); ctx.stroke();
  ctx.setLineDash([]);
}

function renderBtTrades(trades) {
  const tbody = $("#bt-trades-body");
  if (!trades.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center">No trades</td></tr>';
    return;
  }
  tbody.innerHTML = trades.slice(0, 50).map(t => {
    const pnlCls = t.pnl >= 0 ? "pnl-pos" : "pnl-neg";
    return `<tr>
      <td>${t.entry_time?.substring(0, 16) || ""}</td>
      <td>${t.exit_time?.substring(0, 16) || ""}</td>
      <td>${t.shares}</td>
      <td>${t.entry_price?.toFixed(2)}</td>
      <td>${t.reason}</td>
      <td class="${pnlCls}">${fmt$(t.pnl)}</td>
    </tr>`;
  }).join("");
}

/* ---- Screener ---- */
async function runScreener() {
  const btn = $("#btn-screener");
  btn.disabled = true;
  btn.textContent = "Scanning...";
  try {
    const wlRaw = $("#scr-watchlist").value;
    const body = {};
    if (wlRaw) body.watchlist = wlRaw.split(",").map(s => s.trim().toUpperCase()).filter(Boolean);
    const period = $("#scr-period").value;
    const interval = $("#scr-interval").value;
    if (period) body.yf_period = period;
    if (interval) body.yf_interval = interval;
    const rows = await api("POST", "/api/screener", body);
    renderScreener(rows);
    $("#scr-results").style.display = "block";
  } catch(e) {
    alert(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Screener";
  }
}

function renderScreener(rows) {
  const tbody = $("#scr-body");
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6">No results</td></tr>';
    return;
  }
  const liveSyms = state.symbols || {};
  tbody.innerHTML = rows.map(r => {
    const freshCls = r.fresh_entry ? "pnl-pos" : "";
    const liveCfg = liveSyms[r.symbol];
    const stratLabel = liveCfg ? liveCfg.strategy : (state.strategy || "---");
    return `<tr>
      <td>${r.symbol}</td>
      <td>${r.last_close != null ? r.last_close.toFixed(2) : "err"}</td>
      <td>${r.target ?? "---"}</td>
      <td class="${freshCls}">${r.fresh_entry ? "YES" : "no"}</td>
      <td>${stratLabel}</td>
      <td>${r.as_of ? r.as_of.substring(0, 16) : (r.error || "")}</td>
    </tr>`;
  }).join("");
}

/* ---- Tabs ---- */
function switchTab(name) {
  $$("nav button").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".view").forEach(v => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "research") {
    loadBtStrategies();
  }
}

async function loadBtStrategies() {
  try {
    const r = await api("GET", "/api/strategies");
    const sel = $("#bt-strategy");
    const cur = sel.value;
    sel.innerHTML = r.strategies.map(s =>
      `<option value="${s}" ${s === cur ? "selected" : ""}>${s}</option>`
    ).join("");
  } catch(_) {}
}

/* ---- Clock ---- */
function tickClock() {
  const now = new Date();
  const et = new Date(now.toLocaleString("en-US", { timeZone: "America/New_York" }));
  const hh = String(et.getHours()).padStart(2, "0");
  const mm = String(et.getMinutes()).padStart(2, "0");
  const ss = String(et.getSeconds()).padStart(2, "0");
  setText("#hdr-clock", `${hh}:${mm}:${ss}`);
}

/* ---- Helpers ---- */
function fmt$(n) {
  if (n == null || isNaN(n)) return "---";
  return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function setText(sel, txt) { const el = $(sel); if (el) el.textContent = txt; }

/* ---- Init ---- */
document.addEventListener("DOMContentLoaded", () => {
  connectSSE();
  loadJournal();
  loadRisk();
  tickClock();
  setInterval(tickClock, 1000);
  switchTab("live");
});
