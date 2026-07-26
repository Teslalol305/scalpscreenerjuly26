/* TapeScreen frontend: ws bridge -> grid / signal feed / drawer / stats.
   Vanilla JS, no build step. Talks to /ws (json), /stats. */
"use strict";

const $ = (id) => document.getElementById(id);
const gridBody = $("grid-body");
const feedList = $("feed-list");

const state = {
  symbols: [],
  rows: new Map(),        // sym -> {tr, cells, spark: [], sparkTs: 0, lastPrice: 0}
  signals: [],            // newest last, capped
  filters: { symbol: "", tier: "", rule: "" },
  soundOn: false,
  alertScore: 80,
  watchScore: 60,
  drawer: { sym: null, tf: "1s", timer: null, charts: null },
  ws: null,
  wsRetry: 1000,
};

/* ---------------- formatting ---------------- */

function fmtPrice(p) {
  if (!p) return "–";
  if (p >= 1000) return p.toLocaleString("en-US", { maximumFractionDigits: 1 });
  if (p >= 10) return p.toFixed(2);
  if (p >= 0.1) return p.toFixed(4);
  return p.toPrecision(4);
}
function fmtPct(v) {
  const s = v > 0 ? "+" : "";
  return s + v.toFixed(2) + "%";
}
function fmtSigned(v, dp = 2) {
  return (v > 0 ? "+" : "") + v.toFixed(dp);
}
function pctClass(v) { return v > 0.0005 ? "num-up" : v < -0.0005 ? "num-dn" : ""; }
function fmtClock(ts) {
  return new Date(ts * 1000).toTimeString().slice(0, 8);
}
function fmtUptime(s) {
  if (s < 90) return Math.round(s) + "s";
  if (s < 5400) return Math.round(s / 60) + "m";
  return (s / 3600).toFixed(1) + "h";
}

/* ---------------- grid ---------------- */

const COLS = ["price", "pct1", "pct5", "pct15", "volz", "cvd", "imb", "spr", "fund", "doi", "sl", "ss"];

function buildRow(sym) {
  const tr = document.createElement("tr");
  tr.dataset.sym = sym;
  const cells = {};
  const td0 = document.createElement("td");
  td0.innerHTML = `${sym}<span class="badges"></span>`;
  tr.appendChild(td0);
  cells.badges = td0.querySelector(".badges");
  for (const c of COLS) {
    const td = document.createElement("td");
    td.className = "c-" + c;
    tr.appendChild(td);
    cells[c] = td;
  }
  cells.cvd.innerHTML = `<canvas class="spark" width="96" height="22"></canvas>`;
  cells.imb.innerHTML = `<span class="imb-bar"><i class="b"></i><i class="a"></i></span><span class="imb-txt"></span>`;
  // persistent nodes: rebuilding these via innerHTML every push would reset the
  // browser's hover-tooltip timer, so the 7d-percentile title could never show
  cells.fund.innerHTML = `<span class="fund-badge"></span>`;
  cells.fundBadge = cells.fund.querySelector(".fund-badge");
  for (const c of ["sl", "ss"]) {
    cells[c].innerHTML = `<span class="scorecell"></span>`;
    cells[c + "Span"] = cells[c].querySelector(".scorecell");
  }
  tr.addEventListener("click", () => openDrawer(sym));
  gridBody.appendChild(tr);
  state.rows.set(sym, { tr, cells, spark: [], sparkTs: 0, lastPrice: 0 });
}

function flash(td, dir) {
  td.classList.remove("flash-up", "flash-dn");
  void td.offsetWidth; // restart animation
  td.classList.add(dir > 0 ? "flash-up" : "flash-dn");
  td.addEventListener("animationend", () => td.classList.remove("flash-up", "flash-dn"),
    { once: true });
}

function drawSpark(canvas, data) {
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  if (data.length < 2) return;
  const min = Math.min(...data), max = Math.max(...data);
  const span = max - min || 1;
  ctx.beginPath();
  data.forEach((v, i) => {
    const x = (i / (data.length - 1)) * (w - 2) + 1;
    const y = h - 2 - ((v - min) / span) * (h - 4);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  const last = data[data.length - 1], first = data[0];
  ctx.strokeStyle = last >= first ? "#26a69a" : "#ef5350";
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

function scoreBg(score, isLong) {
  const a = Math.min(1, score / 100) * 0.55;
  return isLong ? `rgba(38,166,154,${a})` : `rgba(239,83,80,${a})`;
}

function updateRow(sym, r, now) {
  const row = state.rows.get(sym);
  if (!row) return;
  const { cells, tr } = row;
  tr.classList.toggle("stale", !!r.stale);

  if (r.price !== row.lastPrice && row.lastPrice > 0) flash(cells.price, r.price - row.lastPrice);
  row.lastPrice = r.price;
  cells.price.textContent = fmtPrice(r.price);

  cells.pct1.textContent = fmtPct(r.pct_1m);
  cells.pct1.className = "c-pct1 " + pctClass(r.pct_1m / 100);
  cells.pct5.textContent = fmtPct(r.pct_5m);
  cells.pct5.className = "c-pct5 " + pctClass(r.pct_5m / 100);
  cells.pct15.textContent = fmtPct(r.pct_15m);
  cells.pct15.className = "c-pct15 " + pctClass(r.pct_15m / 100);
  cells.volz.textContent = r.vol_z.toFixed(1);
  cells.volz.style.fontWeight = Math.abs(r.vol_z) >= 2.5 ? "700" : "400";

  if (now - row.sparkTs >= 1) {
    row.sparkTs = now;
    row.spark.push(r.cvd_5m);
    if (row.spark.length > 300) row.spark.shift();
    drawSpark(cells.cvd.querySelector("canvas"), row.spark);
  }

  const bid = Math.round(r.imb * 82);
  const bar = cells.imb.querySelector(".imb-bar");
  bar.querySelector(".b").style.width = bid + "px";
  bar.querySelector(".a").style.width = (82 - bid) + "px";
  cells.imb.querySelector(".imb-txt").textContent = r.imb.toFixed(2);

  cells.spr.textContent = r.spread_bps.toFixed(1);
  const fb = r.funding * 1e4;
  const badge = cells.fundBadge;
  badge.textContent = `${fmtSigned(fb, 2)}bp`;
  badge.className = `fund-badge ${r.funding_bias ? "hot" : ""}`;
  badge.title = `7d pctl ${r.funding_pctl}${r.funding_bias ? " · crowded, bias " + r.funding_bias : ""}`;
  cells.doi.textContent = fmtSigned(r.doi_5m, Math.abs(r.doi_5m) >= 100 ? 0 : 2);

  for (const [span, score, isLong] of [[cells.slSpan, r.score_long, true], [cells.ssSpan, r.score_short, false]]) {
    const tier = score >= state.alertScore ? "alert" : score >= state.watchScore ? "watch" : "";
    span.className = `scorecell ${tier}`;
    span.style.background = scoreBg(score, isLong);
    span.textContent = Math.round(score);
  }

  let badges = "";
  if (r.unavailable) badges += `<span class="badge-stale" title="not tradable on the venue">n/a</span>`;
  if (r.warming) badges += `<span class="badge-warm" title="baselines still warming">warm</span>`;
  if (r.stale && !r.unavailable) badges += `<span class="badge-stale">stale</span>`;
  if (r.oi_compression) badges += `<span class="badge-oi" title="OI building while price flat">OI</span>`;
  if (cells.badges.innerHTML !== badges) cells.badges.innerHTML = badges;
  row.sortKey = Math.max(r.score_long, r.score_short);
}

let lastOrder = "";
function resortGrid() {
  const rows = [...state.rows.values()].sort((a, b) => (b.sortKey || 0) - (a.sortKey || 0));
  const order = rows.map((r) => r.tr.dataset.sym).join(",");
  if (order === lastOrder) return; // re-appending restarts CSS animations; skip when unchanged
  lastOrder = order;
  rows.forEach((r) => gridBody.appendChild(r.tr));
}

/* ---------------- status bar ---------------- */

function updateStatus(st) {
  const fh = st.feed_health || {};
  const connected = fh.connected !== undefined ? fh.connected : true;
  const dot = $("st-conn").querySelector(".dot");
  dot.className = "dot " + (connected ? "ok" : "bad");
  $("st-conn").querySelector("b").textContent = fh.venue || "replay";
  $("st-rate").textContent = st.msg_rate ?? 0;
  $("st-ingest").textContent = `${st.ingest_latency_p50_ms}/${st.ingest_latency_p95_ms}ms`;
  $("st-pipe").textContent = st.pipeline_latency_p95_ms
    ? `${st.pipeline_latency_p50_ms}/${st.pipeline_latency_p95_ms}ms` : "–";
  $("st-drops").textContent = (fh.dropped_msgs || 0) + (fh.queue_drops || 0);
  $("st-uptime").textContent = fmtUptime(st.uptime_s || 0);
}

/* ---------------- signal feed ---------------- */

function passesFilter(s) {
  const f = state.filters;
  return (!f.symbol || s.symbol === f.symbol) &&
         (!f.tier || s.tier === f.tier) &&
         (!f.rule || s.rule === f.rule);
}

function renderSignal(s) {
  const li = document.createElement("li");
  const snap = s.snapshot || {};
  const inv = snap.invalidation ? ` inv ${fmtPrice(snap.invalidation)}` : "";
  li.innerHTML = `
    <div class="l1">
      <span class="tier ${s.tier}">${s.tier}</span>
      <span class="sym">${s.symbol}</span>
      <span class="side-${s.side}">${s.side}</span>
      <span class="rule">${s.rule}</span>
      <span class="t">${fmtClock(s.ts)}</span>
    </div>
    <div class="snap">score ${s.score} · str ${s.strength} · px ${fmtPrice(snap.price || 0)} ·
      vz ${(snap.vol_z ?? 0).toFixed(1)} · cvd1m ${fmtSigned(snap.cvd_1m ?? 0)} ·
      imb ${(snap.book_imbalance ?? 0.5).toFixed(2)} · spr ${(snap.spread_bps ?? 0).toFixed(1)}bp${inv}</div>`;
  return li;
}

function refreshFeed() {
  feedList.textContent = "";
  const shown = state.signals.filter(passesFilter).slice(-150).reverse();
  for (const s of shown) feedList.appendChild(renderSignal(s));
}

function onSignal(s, live) {
  state.signals.push(s);
  if (state.signals.length > 600) state.signals.shift();
  if (!document.querySelector(`#f-rule option[value="${s.rule}"]`)) {
    const o = document.createElement("option");
    o.value = o.textContent = s.rule;
    $("f-rule").appendChild(o);
  }
  if (passesFilter(s)) {
    feedList.prepend(renderSignal(s));
    while (feedList.children.length > 150) feedList.lastChild.remove();
  }
  if (live && s.tier === "ALERT") alertUser(s);
}

/* ---------------- alerts (ALERT tier only) ---------------- */

let audioCtx = null;
function beep() {
  audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === "suspended") audioCtx.resume(); // ctx created w/o user gesture
  const t0 = audioCtx.currentTime;
  for (const [f, d] of [[880, 0], [1174, 0.16]]) {
    const osc = audioCtx.createOscillator(), g = audioCtx.createGain();
    osc.frequency.value = f;
    g.gain.setValueAtTime(0.12, t0 + d);
    g.gain.exponentialRampToValueAtTime(0.001, t0 + d + 0.14);
    osc.connect(g).connect(audioCtx.destination);
    osc.start(t0 + d); osc.stop(t0 + d + 0.15);
  }
}

function alertUser(s) {
  if (!state.soundOn) return;
  beep();
  if (Notification.permission === "granted") {
    new Notification(`TapeScreen ALERT · ${s.symbol} ${s.side}`, {
      body: `${s.rule} · score ${s.score} @ ${fmtPrice(s.snapshot?.price || 0)}`,
      tag: `ts-${s.symbol}-${s.side}`,
    });
  }
}

function setSound(on, interactive) {
  state.soundOn = on;
  $("sound-toggle").textContent = on ? "🔔" : "🔇";
  $("sound-toggle").classList.toggle("on", on);
  if (on && interactive) {
    beep(); // audible confirmation only on a real user gesture
    if (Notification.permission === "default") Notification.requestPermission();
  }
}

$("sound-toggle").addEventListener("click", () => {
  const on = !state.soundOn;
  localStorage.setItem("ts-sound", on ? "1" : "0");
  setSound(on, true);
});

/* ---------------- drawer (lightweight-charts) ---------------- */

const CHART_OPTS = {
  autoSize: true,
  layout: { background: { color: "#151a23" }, textColor: "#8a93a6", fontSize: 11 },
  grid: { vertLines: { color: "#1b2230" }, horzLines: { color: "#1b2230" } },
  timeScale: { timeVisible: true, secondsVisible: true, borderColor: "#232b3a" },
  rightPriceScale: { borderColor: "#232b3a" },
  crosshair: { mode: 0 },
};

function makeCharts() {
  const LWC = window.LightweightCharts;
  const priceChart = LWC.createChart($("chart-price"), CHART_OPTS);
  const candles = priceChart.addSeries(LWC.CandlestickSeries, {
    upColor: "#26a69a", downColor: "#ef5350", wickUpColor: "#26a69a",
    wickDownColor: "#ef5350", borderVisible: false,
  });
  const cvdChart = LWC.createChart($("chart-cvd"), CHART_OPTS);
  const cvdSeries = cvdChart.addSeries(LWC.BaselineSeries, {
    baseValue: { type: "price", price: 0 },
    topLineColor: "#26a69a", bottomLineColor: "#ef5350",
    topFillColor1: "rgba(38,166,154,.25)", topFillColor2: "rgba(38,166,154,.02)",
    bottomFillColor1: "rgba(239,83,80,.02)", bottomFillColor2: "rgba(239,83,80,.25)",
    lineWidth: 2,
  });
  priceChart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (r) cvdChart.timeScale().setVisibleLogicalRange(r);
  });
  const markers = LWC.createSeriesMarkers ? LWC.createSeriesMarkers(candles, []) : null;
  return { priceChart, candles, cvdChart, cvdSeries, markers, priceLines: [] };
}

function destroyCharts() {
  const c = state.drawer.charts;
  if (!c) return;
  c.priceChart.remove();
  c.cvdChart.remove();
  state.drawer.charts = null;
}

function openDrawer(sym) {
  state.drawer.sym = sym;
  $("drawer-sym").textContent = sym + " · hyperliquid perp";
  $("drawer").hidden = false;
  destroyCharts();
  state.drawer.charts = makeCharts();
  requestCandles();
  clearInterval(state.drawer.timer);
  state.drawer.timer = setInterval(requestCandles, 2000);
}

function closeDrawer() {
  clearInterval(state.drawer.timer);
  state.drawer.sym = null;
  $("drawer").hidden = true;
  destroyCharts();
}
$("drawer-close").addEventListener("click", closeDrawer);
document.querySelectorAll(".tf").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tf").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  state.drawer.tf = b.dataset.tf;
  requestCandles();
}));

function requestCandles() {
  if (state.drawer.sym && state.ws?.readyState === 1) {
    state.ws.send(JSON.stringify({ type: "candles", symbol: state.drawer.sym }));
  }
}

function onCandles(msg) {
  const c = state.drawer.charts;
  if (!c || msg.symbol !== state.drawer.sym) return;
  const bars = state.drawer.tf === "1m" ? msg.bars_1m : msg.bars_1s;
  const data = bars.map((b) => ({
    time: b[0], open: b[1], high: b[2], low: b[3], close: b[4],
  }));
  c.candles.setData(data);
  let cum = 0;
  c.cvdSeries.setData(bars.map((b) => ({ time: b[0], value: (cum += b[6]) })));

  for (const pl of c.priceLines) c.candles.removePriceLine(pl);
  c.priceLines = [];
  const v = msg.vwap;
  if (v && v.session > 0) {
    const mk = (price, title, color, style) =>
      c.priceLines.push(c.candles.createPriceLine({
        price, title, color, lineStyle: style, lineWidth: 1, axisLabelVisible: false,
      }));
    const S = window.LightweightCharts.LineStyle;
    mk(v.session, "vwap", "#4f8ff7", S.Solid);
    for (const k of [1, 2]) {
      mk(v.session + k * v.sd, `+${k}σ`, "#4f8ff7", S.SparseDotted);
      mk(v.session - k * v.sd, `-${k}σ`, "#4f8ff7", S.SparseDotted);
    }
  }
  if (c.markers) {
    c.markers.setMarkers(msg.signals.filter((s) => s.tier !== "INFO").map((s) => ({
      time: Math.floor(s.ts),
      position: s.side === "long" ? "belowBar" : "aboveBar",
      color: s.side === "long" ? "#26a69a" : "#ef5350",
      shape: s.side === "long" ? "arrowUp" : "arrowDown",
      text: s.rule.split("_")[0],
    })));
  }
}

/* ---------------- stats tab ---------------- */

function tbl(rows, cols) {
  if (!rows?.length) return "<p class='note'>no data yet</p>";
  let h = "<table class='stats-table'><tr><th></th>" + cols.map((c) => `<th>${c[0]}</th>`).join("") + "</tr>";
  for (const r of rows) {
    h += `<tr><td>${r.grp}</td>` + cols.map((c) => `<td>${c[1](r)}</td>`).join("") + "</tr>";
  }
  return h + "</table>";
}

async function loadStats() {
  const r = await fetch("/stats");
  if (!r.ok) { $("stats-totals").innerHTML = "<p class='note'>stats unavailable</p>"; return; }
  const d = await r.json();
  const pct = (v) => (v == null ? "–" : (100 * v).toFixed(0) + "%");
  const num = (v, dp) => (v == null ? "–" : Number(v).toFixed(dp));
  $("stats-totals").innerHTML = `
    <div class="stat-tile"><div class="v">${d.totals.signals ?? 0}</div><div class="k">signals</div></div>
    <div class="stat-tile"><div class="v">${d.totals.alerts ?? 0}</div><div class="k">alerts</div></div>
    <div class="stat-tile"><div class="v">${d.totals.completed ?? 0}</div><div class="k">outcomes complete</div></div>
    <div class="stat-tile"><div class="v">${d.haircut_bps}bp</div><div class="k">haircut</div></div>`;
  const cols = [
    ["n", (r) => r.signals],
    ["alerts", (r) => r.alerts ?? 0],
    ["hit 30s", (r) => pct(r.hit_ret_30s)], ["n", (r) => r.n_ret_30s ?? 0],
    ["hit 1m", (r) => pct(r.hit_ret_1m)], ["n", (r) => r.n_ret_1m ?? 0],
    ["hit 3m", (r) => pct(r.hit_ret_3m)],
    ["hit 5m", (r) => pct(r.hit_ret_5m)],
    ["avg MFE", (r) => num(r.avg_mfe, 4)],
    ["avg MAE", (r) => num(r.avg_mae, 4)],
  ];
  $("stats-rule").innerHTML = tbl(d.by_rule, cols);
  $("stats-symbol").innerHTML = tbl(d.by_symbol, cols);
}

document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  const stats = b.dataset.tab === "stats";
  $("view-screen").hidden = stats;
  $("view-stats").hidden = !stats;
  if (stats) loadStats();
}));

/* ---------------- filters ---------------- */

for (const [id, key] of [["f-symbol", "symbol"], ["f-tier", "tier"], ["f-rule", "rule"]]) {
  $(id).addEventListener("change", (e) => {
    state.filters[key] = e.target.value;
    refreshFeed();
  });
}

/* ---------------- websocket ---------------- */

function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  state.ws = ws;
  ws.onopen = () => {
    state.wsRetry = 1000;
    $("disconnect-banner").hidden = true;
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "grid") {
      const now = msg.ts;
      for (const [sym, r] of Object.entries(msg.rows)) updateRow(sym, r, now);
      resortGrid();
      updateStatus(msg.status);
    } else if (msg.type === "signal") {
      onSignal(msg.row, true);
    } else if (msg.type === "candles") {
      onCandles(msg);
    } else if (msg.type === "hello") {
      onHello(msg);
    }
  };
  ws.onclose = () => {
    $("disconnect-banner").hidden = false;
    setTimeout(connect, state.wsRetry);
    state.wsRetry = Math.min(state.wsRetry * 2, 15000);
  };
}

function onHello(msg) {
  state.alertScore = msg.alert_score;
  state.watchScore = msg.watch_score;
  if (msg.symbols.join(",") !== state.symbols.join(",")) {
    // fresh build OR the server restarted with a different watchlist:
    // rebuild so removed symbols don't linger as frozen ghost rows
    gridBody.textContent = "";
    state.rows.clear();
    lastOrder = "";
    $("f-symbol").querySelectorAll("option:not([value=''])").forEach((o) => o.remove());
    state.symbols = msg.symbols;
    for (const sym of msg.symbols) {
      buildRow(sym);
      const o = document.createElement("option");
      o.value = o.textContent = sym;
      $("f-symbol").appendChild(o);
    }
  }
  state.signals = [];
  for (const s of msg.recent_signals || []) onSignal(s, false);
  refreshFeed();
  const saved = localStorage.getItem("ts-sound");
  setSound(saved === null ? msg.sound_default : saved === "1", false);
}

connect();
