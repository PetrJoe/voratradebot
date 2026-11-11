"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "–" : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const cls = (v) => v > 0 ? "text-up" : v < 0 ? "text-down" : "";
const when = (iso) => iso ? new Date(iso).toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "–";

let state = null, currentTab = "positions", settingsCache = null;

async function api(path, opts = {}) {
  const o = { method: opts.method || "GET", headers: { "X-Requested-With": "vora" } };
  if (opts.body) { o.headers["Content-Type"] = "application/json"; o.body = JSON.stringify(opts.body); }
  const r = await fetch("/api" + path, o);
  let data = null; try { data = await r.json(); } catch { }
  if (!r.ok) { const e = new Error(); e.status = r.status; e.detail = data?.detail ?? data; throw e; }
  return data;
}
function toast(msg, bad = false) {
  const t = $("#toast"); t.textContent = msg; t.style.borderColor = bad ? "#e0635b" : "#28384e";
  t.classList.remove("hidden"); clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 4500);
}
function confirmDialog(title, bodyHtml, okLabel = "Confirm") {
  return new Promise(res => {
    const d = $("#confirm"); $("#confirm-title").textContent = title; $("#confirm-body").innerHTML = bodyHtml; $("#confirm-ok").textContent = okLabel;
    d.onclose = () => res(d.returnValue === "ok"); d.showModal();
  });
}

/* ---------------- header / status ---------------- */
function chip(label, kind, title = "") { return `<span class="chip" title="${esc(title)}"><span class="dot ${kind}"></span>${esc(label)}</span>`; }
function renderStatus(s) {
  const f = s.flags, m = s.mt5, chips = [];
  chips.push(chip(f.connected ? "MT5 connected" : "MT5 connection lost", f.connected ? "ok" : "bad", m.message));
  chips.push(chip(!s.groq.configured ? "Groq key missing" : s.groq.available === false ? "Groq unavailable" : s.groq.available ? "Groq ready" : "Groq unchecked",
    s.groq.available ? "ok" : (s.groq.available === false || !s.groq.configured) ? "bad" : "wait", `${s.groq.model}: ${s.groq.message}`));
  chips.push(chip(m.demo === true ? "Demo account" : m.demo === false ? "REAL account" : "Account unverified", m.demo === true ? "ok" : "bad", m.login ? `#${m.login} @ ${m.server}` : ""));
  chips.push(chip(s.auto_trading ? "Auto trading on" : "Auto trading off", s.auto_trading ? "wait" : ""));
  $("#status-chips").innerHTML = chips.join("");
  $("#hdr-symbol").textContent = `${s.symbol || "Symbol not resolved"} · ${s.timeframe}`;

  let txt = "Stopped", tone = "";
  if (f.emergency) { txt = "Emergency stop: no new orders"; tone = "bad"; }
  else if (!f.connected) { txt = "Connection lost: not trading"; tone = "bad"; }
  else if (f.risk_locked) { txt = "Risk locked: " + (s.account?.lock_reason || "daily limit"); tone = "bad"; }
  else if (f.running) { txt = f.ai_unavailable ? "Running · AI unavailable (no trades will be placed)" : "Running"; tone = f.ai_unavailable ? "wait" : "ok"; }
  else if (f.paused) { txt = "Paused: monitoring only"; tone = "wait"; }
  const b = $("#state-banner"); b.innerHTML = `<span class="dot ${tone} inline-block mr-2"></span>${esc(txt)}`;
  const st = s.bot_state;
  $("#btn-start").disabled = st === "RUNNING" || st === "PAUSED";
  $("#btn-start").textContent = st === "EMERGENCY_STOP" ? "Clear stop & start" : "Start bot";
  $("#btn-pause").disabled = st !== "RUNNING"; $("#btn-resume").disabled = st !== "PAUSED";
  $("#btn-stop").disabled = st === "STOPPED";
  $("#auto-toggle").checked = s.auto_trading;
  $("#auto-note").textContent = s.auto_trading ? "On: approved setups are sent as real orders to the account" : "Off: signals are analysed but no orders are sent";
  $("#permit-note").textContent = s.trading_permitted ? "" : "Trading blocked: " + s.not_permitted_reasons.join("; ");
}
function renderAccount(a) {
  if (!a) { $("#account").innerHTML = `<div class="col-span-2 text-mute">Waiting for MT5…</div>`; return; }
  const row = (k, v, c = "") => `<dt class="text-mute">${k}</dt><dd class="num text-right ${c}">${v}</dd>`;
  $("#account").innerHTML = row("Balance", fmt(a.balance)) + row("Equity", fmt(a.equity)) + row("Free margin", fmt(a.free_margin)) +
    row("Used margin", fmt(a.margin)) + row("Floating P/L", fmt(a.floating), cls(a.floating)) + row("Daily P/L", fmt(a.daily_pnl), cls(a.daily_pnl)) +
    row("Currency", esc(a.currency)) + row("Leverage", "1:" + a.leverage);
  const pct = a.daily_limit ? Math.max(0, Math.min(100, a.daily_remaining / a.daily_limit * 100)) : 100;
  $("#limit-txt").textContent = `${fmt(a.daily_remaining)} of ${fmt(a.daily_limit)}`;
  const bar = $("#limit-bar"); bar.className = "bar " + (pct < 25 ? "bad" : pct < 55 ? "warn" : ""); bar.firstElementChild.style.width = pct + "%";
}
function renderMarket(m, s) {
  if (!m) { $("#market").innerHTML = `<div class="col-span-full text-mute">${esc(s.symbol_error || "Waiting for market data…")}</div>`; return; }
  const d = m.digits ?? 2, c = (k, v, extra = "") => `<div><div class="text-xs text-mute">${k}</div><div class="num ${extra}">${v}</div></div>`;
  const tcol = m.trend === "UPTREND" ? "text-up" : m.trend === "DOWNTREND" ? "text-down" : "text-mute";
  $("#market").innerHTML = c("Instrument", esc(m.symbol), "text-xs break-words") + c("Price", fmt(m.price, d), "text-lg") + c("Bid", fmt(m.bid, d)) + c("Ask", fmt(m.ask, d)) +
    c("Spread", fmt(m.spread, d)) + c("Trend", m.trend.toLowerCase(), tcol) + c("RSI 14", fmt(m.rsi, 1)) + c("ATR 14 · ADX 14", `${fmt(m.atr, d)} · ${fmt(m.adx, 1)}`) +
    (m.data_ok ? "" : `<div class="col-span-full text-xs text-warn">Market data is stale or incomplete: the bot will not trade.</div>`);
}
function renderAI(ai, s) {
  const p = $("#ai-panel");
  if (!ai) { p.innerHTML = `<div class="text-mute">No setup has been sent to Groq yet.${s.last_analysis ? `<br>Last candle check: buy ${fmt(s.last_analysis.buy_score, 0)} / sell ${fmt(s.last_analysis.sell_score, 0)} (needs a qualified setup).` : ""}</div>`; return; }
  const col = ai.decision === "APPROVE" ? "text-up" : ai.decision === "ERROR" ? "text-down" : "text-warn";
  p.innerHTML = `<div class="flex items-baseline justify-between"><span class="${col} font-semibold">${esc(ai.decision)}</span><span class="num text-mute">${ai.confidence != null ? fmt(ai.confidence, 0) + "% confidence" : ""}</span></div>
    <div class="text-xs text-mute">Proposed ${esc(ai.direction)} · risk ${esc(ai.risk_assessment || "–")} · ${esc(when(ai.time))}</div>
    <p class="text-sm leading-relaxed">${esc(ai.reasoning)}</p>
    ${(ai.warnings || []).length ? `<ul class="text-xs text-warn list-disc pl-4">${ai.warnings.map(w => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
    <div class="text-xs ${ai.accepted ? "text-up" : "text-mute"}">${ai.accepted ? "Passed Python validation" : "Not used: " + esc(ai.rejection_reason || "")}</div>`;
}
function render(s) {
  state = s; renderStatus(s); renderAccount(s.account); renderMarket(s.market, s); renderAI(s.last_ai, s);
  if (currentTab === "positions") renderPositions();
}

/* ---------------- tabs ---------------- */
const table = (cols, rows, empty) => rows.length ? `<table><thead><tr>${cols.map(c => `<th>${c[0]}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${cols.map(c => `<td class="${c[2] ? c[2](r) : ""}">${c[1](r)}</td>`).join("")}</tr>`).join("")}</tbody></table>` : `<div class="p-4 text-sm text-mute">${empty}</div>`;
const dirCol = r => r.direction === "BUY" ? "text-up" : "text-down";

function renderPositions() {
  const d = state?.market?.digits ?? 2;
  $("#tab-body").innerHTML = table([
    ["Ticket", r => `<span class="num">${r.ticket}</span>`], ["Instrument", r => esc(r.symbol)],
    ["Direction", r => r.direction + (r.ours ? "" : " (manual)"), dirCol], ["Volume", r => `<span class="num">${r.volume}</span>`],
    ["Entry", r => `<span class="num">${fmt(r.price_open, d)}</span>`], ["Current", r => `<span class="num">${fmt(r.price_current, d)}</span>`],
    ["Stop loss", r => `<span class="num">${r.sl ? fmt(r.sl, d) : "NONE"}</span>`, r => r.sl ? "" : "text-down"], ["Take profit", r => `<span class="num">${r.tp ? fmt(r.tp, d) : "NONE"}</span>`],
    ["Floating P/L", r => `<span class="num">${fmt(r.profit)}</span>`, r => cls(r.profit)], ["Opened", r => when(new Date(r.time * 1000).toISOString())],
  ], state?.positions || [], "No open positions.");
}
async function renderTrades() {
  const rows = await api("/trades");
  $("#tab-body").innerHTML = table([
    ["ID", r => r.id], ["Direction", r => r.direction, dirCol], ["Entry", r => `<span class="num">${fmt(r.entry_price, 2)}</span>`],
    ["Exit", r => `<span class="num">${r.exit_price != null ? fmt(r.exit_price, 2) : "–"}</span>`], ["Volume", r => `<span class="num">${r.volume}</span>`],
    ["P/L", r => `<span class="num">${r.profit != null ? fmt(r.profit) : "–"}</span>`, r => cls(r.profit)],
    ["Outcome", r => r.status === "open" ? "open" : (r.profit > 0 ? "win" : r.profit < 0 ? "loss" : "breakeven") + (r.exit_reason ? ` (${esc(r.exit_reason)})` : "")],
    ["Entry reason", r => `<div class="max-w-[22rem] text-mute">${esc(r.strategy_reason)}</div>`], ["AI decision", r => `<div class="max-w-[18rem] text-mute">${esc(r.ai_decision)}</div>`],
    ["Opened", r => esc(when(r.opened_at))],
  ], rows, "No trades yet.");
}
async function renderSignals() {
  const rows = await api("/signals");
  $("#tab-body").innerHTML = table([
    ["Time", r => esc(when(r.created_at))], ["Direction", r => r.direction, dirCol], ["Score", r => `<span class="num">${fmt(r.score, 0)}</span>`],
    ["Technical evidence", r => `<div class="max-w-[26rem] text-mute">${esc((r.reason || "").split(" || ")[0])}</div>`],
    ["Groq decision", r => r.ai ? `${esc(r.ai.decision)} ${r.ai.confidence != null ? fmt(r.ai.confidence, 0) + "%" : ""}` : "not asked"],
    ["Status / rejection reason", r => `<div class="max-w-[22rem]">${esc(r.status)}<span class="text-mute"> ${esc((r.reason || "").split(" || ").slice(1).join(" · "))}</span></div>`],
    ["Executed", r => r.executed ? "yes" : "no", r => r.executed ? "text-up" : "text-mute"],
  ], rows, "No candidate setups yet. The bot only records a signal when a candle closes with enough confluence.");
}
async function renderEvents() {
  const rows = await api("/events");
  $("#tab-body").innerHTML = table([["Time", r => esc(when(r.created_at))], ["Type", r => esc(r.event_type)], ["Message", r => esc(r.message)]], rows, "No events.");
}

/* ---------------- settings ---------------- */
async function renderSettings() {
  const body = $("#tab-body"); body.innerHTML = `<div class="p-4 text-mute text-sm">Loading…</div>`;
  settingsCache = await api("/settings");
  const groups = {}; settingsCache.schema.forEach(f => (groups[f.group] ||= []).push(f));
  const input = f => {
    const v = settingsCache.values[f.key], id = "set-" + f.key;
    if (f.type === "bool") return `<input type="checkbox" id="${id}" data-key="${f.key}" ${v ? "checked" : ""} class="h-5 w-5 accent-[#d4a84f]">`;
    if (f.type === "str") return `<input type="text" id="${id}" data-key="${f.key}" value="${esc(v)}" placeholder="${f.key === "groq_model" ? esc(settingsCache.effective_groq_model) : ""}">`;
    return `<input type="number" id="${id}" data-key="${f.key}" value="${v}" step="${f.type === "int" ? 1 : "any"}" ${f.min != null ? `min="${f.min}"` : ""} ${f.max != null ? `max="${f.max}"` : ""}>`;
  };
  body.innerHTML = `<form id="settings-form" class="p-4 grid gap-5">
    <p class="text-xs text-mute">Risk controls (stop loss, take profit, daily loss limit, risk per trade) always stay on; you can only tune them within safe bounds. Changes that increase risk ask for confirmation. Auto trading is changed with the toggle on the right.</p>
    ${Object.entries(groups).map(([g, fs]) => `<fieldset class="grid gap-2"><legend class="text-sm font-semibold mb-1">${esc(g)}</legend>
      <div class="grid sm:grid-cols-2 xl:grid-cols-3 gap-x-4 gap-y-3">${fs.map(f => `<div><label class="text-xs text-mute block mb-1" for="set-${f.key}">${esc(f.label)}${f.danger ? ` <span class="text-warn">· confirm to loosen</span>` : ""}</label>${input(f)}${f.desc ? `<div class="text-[11px] text-mute mt-1">${esc(f.desc)}</div>` : ""}</div>`).join("")}</div></fieldset>`).join("")}
    <div class="flex gap-2"><button class="btn btn-go" type="submit">Save settings</button><button class="btn" type="button" id="settings-reset">Discard edits</button></div></form>`;
  $("#settings-reset").onclick = renderSettings;
  $("#settings-form").onsubmit = saveSettings;
}
async function saveSettings(ev) {
  ev.preventDefault();
  const changes = {};
  settingsCache.schema.forEach(f => {
    const el = $("#set-" + f.key); if (!el) return;
    const v = f.type === "bool" ? el.checked : f.type === "str" ? el.value.trim() : Number(el.value);
    if (v !== settingsCache.values[f.key]) changes[f.key] = v;
  });
  if (!Object.keys(changes).length) return toast("No changes to save");
  try { await api("/settings", { method: "PUT", body: { changes, confirm: false } }); toast("Settings saved"); renderSettings(); }
  catch (e) {
    if (e.status === 409 && e.detail?.needs_confirmation) {
      const names = e.detail.needs_confirmation.map(k => settingsCache.schema.find(f => f.key === k)?.label || k);
      if (await confirmDialog("Increase risk?", `These changes make the bot accept more risk or weaker setups:<ul class="list-disc pl-5 mt-2">${names.map(n => `<li>${esc(n)}</li>`).join("")}</ul>`, "Apply anyway")) {
        try { await api("/settings", { method: "PUT", body: { changes, confirm: true } }); toast("Settings saved"); renderSettings(); } catch (e2) { toast(errText(e2), true); }
      }
    } else toast(errText(e), true);
  }
}
const errText = e => typeof e.detail === "string" ? e.detail : e.detail?.error ? e.detail.error + (e.detail.details ? ": " + e.detail.details.map(d => d.loc?.join(".") + " " + d.msg).join("; ") : "") : e.detail?.message || "Request failed";

/* ---------------- backtest ---------------- */
function renderBacktest() {
  const today = new Date().toISOString().slice(0, 10), start = new Date(Date.now() - 14 * 864e5).toISOString().slice(0, 10);
  $("#tab-body").innerHTML = `<div class="p-4 grid gap-4">
    <div class="text-xs text-warn border border-line rounded px-3 py-2 bg-raised">Backtests are simulations on MT5 history using the same rules as the live engine. They are not live or demo results, and Groq is not part of them (AI-assisted backtests would need stored historical AI analyses or many paid API calls).</div>
    <form id="bt-form" class="grid sm:grid-cols-3 xl:grid-cols-6 gap-3 items-end">
      <div><label class="text-xs text-mute block mb-1" for="bt-from">From</label><input type="date" id="bt-from" value="${start}"></div>
      <div><label class="text-xs text-mute block mb-1" for="bt-to">To</label><input type="date" id="bt-to" value="${today}"></div>
      <div><label class="text-xs text-mute block mb-1" for="bt-bal">Start balance</label><input type="number" id="bt-bal" value="10000" min="1"></div>
      <div><label class="text-xs text-mute block mb-1" for="bt-risk">Risk per trade %</label><input type="number" id="bt-risk" value="${state ? "" : 0.5}" step="0.05" min="0.05" max="2" placeholder="current setting"></div>
      <div><label class="text-xs text-mute block mb-1" for="bt-score">Min score</label><input type="number" id="bt-score" min="40" max="100" placeholder="current setting"></div>
      <div><label class="text-xs text-mute block mb-1" for="bt-comm">Commission / lot</label><input type="number" id="bt-comm" value="0" min="0" step="any"></div>
      <div class="sm:col-span-3 xl:col-span-6"><button class="btn btn-go" id="bt-run" type="submit">Run backtest</button> <span class="text-xs text-mute ml-2" id="bt-msg"></span></div>
    </form><div id="bt-result"></div></div>`;
  $("#bt-form").onsubmit = runBacktest;
  api("/backtest/latest").then(r => r && showBacktest(r)).catch(() => { });
}
async function runBacktest(ev) {
  ev.preventDefault(); const btn = $("#bt-run"); btn.disabled = true; $("#bt-msg").textContent = "Running… this can take a minute on long ranges.";
  const overrides = {}; if ($("#bt-risk").value) overrides.risk_per_trade_pct = Number($("#bt-risk").value); if ($("#bt-score").value) overrides.min_score = Number($("#bt-score").value);
  try {
    const r = await api("/backtest", { method: "POST", body: { date_from: $("#bt-from").value + "T00:00:00", date_to: $("#bt-to").value + "T23:59:59", initial_balance: Number($("#bt-bal").value), commission_per_lot: Number($("#bt-comm").value || 0), overrides } });
    showBacktest(r); $("#bt-msg").textContent = "";
  } catch (e) { $("#bt-msg").textContent = ""; toast(errText(e), true); } finally { btn.disabled = false; }
}
let btChart = null;
function showBacktest(r) {
  const k = (a, b, c = "") => `<div><div class="text-xs text-mute">${a}</div><div class="num ${c}">${b}</div></div>`;
  $("#bt-result").innerHTML = `<div class="text-xs text-mute mb-2">${esc(r.kind)} · ${esc(r.symbol)} ${esc(r.timeframe)} · ${r.candles} candles</div>
   <div class="grid grid-cols-2 sm:grid-cols-4 xl:grid-cols-6 gap-3 text-sm">
    ${k("Total trades", r.total_trades)}${k("Wins / losses", `${r.winning_trades} / ${r.losing_trades}`)}${k("Win rate", fmt(r.win_rate) + "%")}
    ${k("Net profit", fmt(r.net_profit), cls(r.net_profit))}${k("Gross profit", fmt(r.gross_profit))}${k("Gross loss", fmt(r.gross_loss))}
    ${k("Profit factor", r.profit_factor == null ? "–" : fmt(r.profit_factor, 2))}${k("Max drawdown", `${fmt(r.max_drawdown)} (${fmt(r.max_drawdown_pct)}%)`)}${k("Avg reward/risk", fmt(r.avg_reward_risk, 2))}
    ${k("Avg duration", fmt(r.avg_trade_duration_min, 0) + " min")}${k("Max consecutive wins", r.max_consecutive_wins)}${k("Max consecutive losses", r.max_consecutive_losses)}</div>
   <div id="bt-chart" class="h-[240px] mt-4"></div>`;
  if (btChart) { btChart.remove(); btChart = null; }
  const el = $("#bt-chart"); btChart = LightweightCharts.createChart(el, chartOpts(el));
  const s = btChart.addLineSeries({ color: "#d4a84f", lineWidth: 2, priceLineVisible: false });
  const seen = new Set(); s.setData(r.equity_curve.filter(p => !seen.has(p.time) && seen.add(p.time)).map(p => ({ time: p.time, value: p.equity }))); btChart.timeScale().fitContent();
}

/* ---------------- tab router ---------------- */
async function showTab(name) {
  currentTab = name;
  document.querySelectorAll(".tab").forEach(t => t.setAttribute("aria-selected", t.dataset.tab === name));
  try {
    if (name === "positions") renderPositions(); else if (name === "trades") await renderTrades(); else if (name === "signals") await renderSignals();
    else if (name === "events") await renderEvents(); else if (name === "backtest") renderBacktest(); else if (name === "settings") await renderSettings();
  } catch (e) { $("#tab-body").innerHTML = `<div class="p-4 text-down text-sm">${esc(errText(e))}</div>`; }
}
document.querySelectorAll(".tab").forEach(t => t.onclick = () => showTab(t.dataset.tab));
setInterval(() => { if (["trades", "signals", "events"].includes(currentTab) && !document.hidden) showTab(currentTab); }, 15000);

/* ---------------- chart ---------------- */
function chartOpts(el) {
  return { width: el.clientWidth, height: el.clientHeight, layout: { background: { color: "transparent" }, textColor: "#8091a8", fontFamily: "IBM Plex Mono" },
    grid: { vertLines: { color: "#1f2d41" }, horzLines: { color: "#1f2d41" } }, timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#28384e" }, rightPriceScale: { borderColor: "#28384e" } };
}
let chart, candleSeries, emaSeries = {}, priceLines = [];
function initChart() {
  const el = $("#chart"); chart = LightweightCharts.createChart(el, chartOpts(el));
  candleSeries = chart.addCandlestickSeries({ upColor: "#35b38a", downColor: "#e0635b", borderVisible: false, wickUpColor: "#35b38a", wickDownColor: "#e0635b" });
  emaSeries = { ema9: chart.addLineSeries({ color: "#5aa0e6", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }),
    ema21: chart.addLineSeries({ color: "#d4a84f", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }),
    ema50: chart.addLineSeries({ color: "#b48ef0", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }) };
  new ResizeObserver(() => { chart.applyOptions({ width: el.clientWidth }); if (btChart) { const b = $("#bt-chart"); b && btChart.applyOptions({ width: b.clientWidth }); } }).observe(el);
}
let firstFit = true;
async function refreshChart() {
  if (document.hidden || !state?.flags.connected) return;
  try {
    const d = await api(`/candles?timeframe=${$("#chart-tf").value}&count=300`);
    if (!d.candles.length) return;
    candleSeries.setData(d.candles.map(c => ({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close })));
    Object.entries(emaSeries).forEach(([k, s]) => s.setData(d.ema[k] || []));
    const times = new Set(d.candles.map(c => c.time)), tf = { M1: 60, M5: 300, M15: 900, M30: 1800, H1: 3600 }[$("#chart-tf").value];
    const snap = t => { const b = Math.floor(t / tf) * tf; return times.has(b) ? b : null; };
    const marks = d.markers.map(m => ({ t: snap(m.time), m })).filter(x => x.t !== null).sort((a, b) => a.t - b.t).map(({ t, m }) => ({
      time: t, position: m.dir === "BUY" ? "belowBar" : "aboveBar", shape: m.dir === "BUY" ? "arrowUp" : "arrowDown",
      color: m.executed ? (m.dir === "BUY" ? "#35b38a" : "#e0635b") : "#8091a8", text: m.executed ? m.dir : "" }));
    candleSeries.setMarkers(marks);
    priceLines.forEach(l => candleSeries.removePriceLine(l)); priceLines = d.lines.map(l => candleSeries.createPriceLine({
      price: l.price, color: l.kind === "sl" ? "#e0635b" : l.kind === "tp" ? "#35b38a" : "#d4a84f", lineWidth: 1, lineStyle: l.kind === "entry" ? 0 : 2, axisLabelVisible: true, title: l.kind.toUpperCase() }));
    if (firstFit) { chart.timeScale().fitContent(); firstFit = false; }
  } catch { }
}
$("#chart-tf").onchange = () => { firstFit = true; refreshChart(); };
setInterval(refreshChart, 5000);

/* ---------------- controls ---------------- */
async function act(path, body) { try { await api(path, { method: "POST", body }); await poll(); } catch (e) { toast(errText(e), true); } }
$("#btn-start").onclick = async () => {
  if (state?.bot_state === "EMERGENCY_STOP" && !await confirmDialog("Clear emergency stop?", "New orders will be allowed again once the bot runs.", "Clear and start")) return;
  act("/bot/start");
};
$("#btn-stop").onclick = () => act("/bot/stop");
$("#btn-pause").onclick = () => act("/bot/pause");
$("#btn-resume").onclick = () => act("/bot/resume");
$("#btn-estop").onclick = async () => { await act("/bot/emergency-stop"); toast("Emergency stop active: new orders are blocked. Open positions keep their stop loss and take profit."); };
$("#auto-toggle").onchange = async (ev) => {
  const on = ev.target.checked;
  if (on && !await confirmDialog("Enable auto trading?", "The bot will place orders on the connected MT5 account whenever a setup passes every check. Only the demo account is allowed while DEMO_ONLY=true.", "Enable")) { ev.target.checked = false; return; }
  try { await api("/bot/auto-trading", { method: "POST", body: { enabled: on, confirm: on } }); await poll(); } catch (e) { toast(errText(e), true); ev.target.checked = !on; }
};

/* ---------------- live updates ---------------- */
async function poll() { try { render(await api("/status")); } catch { } }
let ws, wsRetry = 1000;
function connectWS() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.type === "state") render(m.data); };
  ws.onopen = () => wsRetry = 1000;
  ws.onclose = () => { setTimeout(connectWS, wsRetry); wsRetry = Math.min(wsRetry * 2, 15000); };
}
setInterval(() => { $("#clock").textContent = new Date().toLocaleTimeString([], { hour12: false }) + " " + Intl.DateTimeFormat().resolvedOptions().timeZone; }, 1000);
setInterval(() => { if (!ws || ws.readyState !== 1) poll(); }, 5000);
initChart(); poll().then(() => refreshChart()); connectWS(); showTab("positions");
