/* TapeScreen frontend v2: glance-first board of entry/exit signals with learned
   confidence, plus the symbol grid, drawer, and stats. Vanilla JS, no build step. */
"use strict";

const $ = (id) => document.getElementById(id);
const gridBody = $("grid-body");
const boardCards = $("board-cards");
const ticker = $("ticker");

const state = {
  symbols: [],
  rows: new Map(),        // sym -> {tr, cells, spark: [], sparkTs: 0, lastPrice: 0}
  cards: new Map(),       // trade id -> card element
  tally: { win: 0, loss: 0 },
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
function fmtPct(v) { return (v > 0 ? "+" : "") + v.toFixed(2) + "%"; }
function fmtSigned(v, dp = 2) { return (v > 0 ? "+" : "") + v.toFixed(dp); }
function pctClass(v) { return v > 0.0005 ? "num-up" : v < -0.0005 ? "num-dn" : ""; }
function fmtClock(ts) { return new Date(ts * 1000).toTimeString().slice(0, 8); }
function fmtUptime(s) {
  if (s < 90) return Math.round(s) + "s";
  if (s < 5400) return Math.round(s / 60) + "m";
  return (s / 3600).toFixed(1) + "h";
}
function shortRule(r) { return r.replace("_ignition", "").replace("_imbalance", " imb").replace("_", " "); }

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
  cells.cvd.innerHTML = `<canvas class="spark" width="90" height="22"></canvas>`;
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
  ctx.strokeStyle = data[data.length - 1] >= data[0] ? "#26a69a" : "#ef5350";
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

  const bid = Math.round(r.imb * 74);
  const bar = cells.imb.querySelector(".imb-bar");
  bar.querySelector(".b").style.width = bid + "px";
  bar.querySelector(".a").style.width = (74 - bid) + "px";
  cells.imb.querySelector(".imb-txt").textContent = r.imb.toFixed(2);

  cells.spr.textContent = r.spread_bps.toFixed(1);
  const badge = cells.fundBadge;
  badge.textContent = `${fmtSigned(r.funding * 1e4, 2)}bp`;
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

/* ---------------- active signals board ---------------- */

function confClass(pct) { return pct >= 58 ? "conf-hi" : pct <= 45 ? "conf-lo" : ""; }

function cardHtml(t) {
  const conf = t.confidence;
  const src = t.conf_src === "model" ? "model" : "history";
  return `
    <div class="r1">
      <span class="side-pill ${t.side}">${t.side.toUpperCase()}</span>
      <span class="sym">${t.symbol}</span>
      <span class="tierchip ${t.tier}">${t.tier}</span>
      <span class="rule">${shortRule(t.rule)}</span>
      <span class="conf ${confClass(conf)}"><div class="pct">${Math.round(conf)}%</div>
        <div class="n">${src} · n=${t.conf_n}</div></span>
    </div>
    <div class="ladder"></div>
    <div class="r2">
      <span class="kv"><span>avg entry</span><b class="v-avg"></b></span>
      <span class="kv"><span>stop <i class="statechip"></i></span><b class="v-stop"></b></span>
      <span class="kv"><span>banked</span><b class="v-banked"></b></span>
      <span class="liveR"></span>
    </div>
    <div class="age-bar"><i></i></div>`;
}

function makeCard(t, fresh) {
  const el = document.createElement("div");
  el.className = `card ${t.side} ${t.confidence >= 58 ? "hi-conf" : ""} ${fresh ? "enter" : ""}`;
  el.dataset.id = t.id;
  el.dataset.ts = t.ts;
  el.innerHTML = cardHtml(t);
  el.addEventListener("click", () => openDrawer(t.symbol));
  return el;
}

function updateCardLive(el, t) {
  // ladder: one chip per tranche, filled dots as adds trigger
  const ladder = el.querySelector(".ladder");
  const ladderHtml = (t.levels || []).map((lv, i) =>
    `<span class="lvl ${lv.filled ? "filled" : ""}">E${i + 1} ${fmtPrice(lv.px)}</span>`
  ).join("");
  if (ladder.innerHTML !== ladderHtml) ladder.innerHTML = ladderHtml;

  el.querySelector(".v-avg").textContent = fmtPrice(t.avg_entry);
  el.querySelector(".v-stop").textContent = fmtPrice(t.stop);
  const chip = el.querySelector(".statechip");
  chip.textContent = t.state === "INIT" ? "" : t.state;
  chip.className = "statechip st-" + t.state;
  el.querySelector(".v-banked").textContent = t.tp1_done ? `+${t.realized}R` : "–";

  const lr = el.querySelector(".liveR");
  lr.textContent = fmtSigned(t.live_r, 2) + "R";
  lr.className = "liveR " + (t.live_r > 0.05 ? "num-up" : t.live_r < -0.05 ? "num-dn" : "");
  const age = Math.min(1, (Date.now() / 1000 - t.ts) / (state.maxHold || 7200));
  el.querySelector(".age-bar i").style.width = (age * 100).toFixed(0) + "%";
}

function renderBoard(active) {
  const seen = new Set();
  for (const t of active) {
    seen.add(String(t.id));
    let el = state.cards.get(String(t.id));
    if (!el) {
      el = makeCard(t, false);
      state.cards.set(String(t.id), el);
      boardCards.appendChild(el);
    }
    updateCardLive(el, t);
  }
  for (const [id, el] of state.cards) {
    if (!seen.has(id)) { el.remove(); state.cards.delete(id); }
  }
  $("board-empty").style.display = state.cards.size ? "none" : "block";
  $("board-count").textContent = state.cards.size ? `${state.cards.size} open` : "";
}

function onEntry(t) {
  if (!state.cards.has(String(t.id))) {
    const el = makeCard(t, true);
    state.cards.set(String(t.id), el);
    boardCards.prepend(el);
    updateCardLive(el, t);
    $("board-empty").style.display = "none";
  }
  if (state.soundOn) blip(660, 0.09);
  if (t.tier === "ALERT") alertUser(t);
}

function tickerLi(t, fresh) {
  const li = document.createElement("li");
  if (fresh) li.className = "fresh";
  const win = t.status === "win";
  li.innerHTML = `
    <span class="res-pill ${t.status}">${win ? "WIN" : "LOSS"}</span>
    <span class="r-val ${win ? "num-up" : "num-dn"}">${fmtSigned(t.r_result, 2)}R</span>
    <span class="sym">${t.symbol}</span>
    <span class="side-${t.side}">${t.side}</span>
    <span class="why">${shortRule(t.rule)} · ${t.exit_reason}</span>
    <span class="t">${fmtClock(t.exit_ts)}</span>`;
  return li;
}

function onExit(t) {
  const el = state.cards.get(String(t.id));
  if (el) { el.remove(); state.cards.delete(String(t.id)); }
  ticker.prepend(tickerLi(t, true));
  while (ticker.children.length > 40) ticker.lastChild.remove();
  state.tally[t.status] = (state.tally[t.status] || 0) + 1;
  const { win = 0, loss = 0 } = state.tally;
  $("ticker-tally").textContent = `session W${win} · L${loss}`;
  if (state.soundOn) blip(t.status === "win" ? 880 : 330, 0.12);
  $("board-empty").style.display = state.cards.size ? "none" : "block";
  $("board-count").textContent = state.cards.size ? `${state.cards.size} open` : "";
}

function renderLearning(learning) {
  const box = $("learn-meters");
  if (!learning?.length) { box.innerHTML = "<span class='note'>no resolved signals yet — confidence at prior</span>"; return; }
  box.innerHTML = learning.map((l) => `
    <div class="meter" title="win rate over ${l.n} resolved signals · avg R = expectancy per trade · model trained on ${l.model_n}">
      <div class="m-top"><span class="m-name">${shortRule(l.rule)}</span>
        <span class="m-val">${l.win_rate == null ? "–" : l.win_rate + "%"}</span></div>
      <div class="m-bar"><i style="width:${l.win_rate || 0}%"></i></div>
      <div class="m-sub">n=${l.n} · ${l.avg_r == null ? "" : "avgR " + (l.avg_r > 0 ? "+" : "") + l.avg_r + " · "}w×${l.weight_mult} · ml:${l.model_n}</div>
    </div>`).join("");
}

/* ---------------- status bar ---------------- */

function updateStatus(st) {
  const fh = st.feed_health || {};
  const connected = fh.connected !== undefined ? fh.connected : true;
  $("st-conn").querySelector(".dot").className = "dot " + (connected ? "ok" : "bad");
  $("st-conn").querySelector("b").textContent = fh.venue || "replay";
  $("st-rate").textContent = st.msg_rate ?? 0;
  $("st-ingest").textContent = `${st.ingest_latency_p50_ms}/${st.ingest_latency_p95_ms}ms`;
  $("st-pipe").textContent = st.pipeline_latency_p95_ms
    ? `${st.pipeline_latency_p50_ms}/${st.pipeline_latency_p95_ms}ms` : "–";
  $("st-drops").textContent = (fh.dropped_msgs || 0) + (fh.queue_drops || 0);
  $("st-uptime").textContent = fmtUptime(st.uptime_s || 0);
}

/* ---------------- alerts & sounds ---------------- */

let audioCtx = null;
function ctx() {
  audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === "suspended") audioCtx.resume(); // ctx created w/o user gesture
  return audioCtx;
}
function blip(freq, dur) {
  const ac = ctx(), t0 = ac.currentTime;
  const osc = ac.createOscillator(), g = ac.createGain();
  osc.frequency.value = freq;
  g.gain.setValueAtTime(0.10, t0);
  g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
  osc.connect(g).connect(ac.destination);
  osc.start(t0); osc.stop(t0 + dur + 0.01);
}
function beep() { blip(880, 0.15); setTimeout(() => blip(1174, 0.15), 160); }

function alertUser(t) {
  if (!state.soundOn) return;
  beep();
  if (Notification.permission === "granted") {
    new Notification(`TapeScreen ALERT · ${t.symbol} ${t.side}`, {
      body: `${t.rule} · conf ${Math.round(t.confidence)}% @ ${fmtPrice(t.entry)}`,
      tag: `ts-${t.symbol}-${t.side}`,
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
  c.candles.setData(bars.map((b) => ({
    time: b[0], open: b[1], high: b[2], low: b[3], close: b[4],
  })));
  let cum = 0;
  c.cvdSeries.setData(bars.map((b) => ({ time: b[0], value: (cum += b[6]) })));

  for (const pl of c.priceLines) c.candles.removePriceLine(pl);
  c.priceLines = [];
  const v = msg.vwap;
  if (v && v.session > 0) {
    const S = window.LightweightCharts.LineStyle;
    const mk = (price, title, style) =>
      c.priceLines.push(c.candles.createPriceLine({
        price, title, color: "#4f8ff7", lineStyle: style, lineWidth: 1, axisLabelVisible: false,
      }));
    mk(v.session, "vwap", S.Solid);
    for (const k of [1, 2]) {
      mk(v.session + k * v.sd, `+${k}σ`, S.SparseDotted);
      mk(v.session - k * v.sd, `-${k}σ`, S.SparseDotted);
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
    ["n", (r2) => r2.signals],
    ["alerts", (r2) => r2.alerts ?? 0],
    ["hit 30s", (r2) => pct(r2.hit_ret_30s)], ["n", (r2) => r2.n_ret_30s ?? 0],
    ["hit 1m", (r2) => pct(r2.hit_ret_1m)], ["n", (r2) => r2.n_ret_1m ?? 0],
    ["hit 3m", (r2) => pct(r2.hit_ret_3m)],
    ["hit 5m", (r2) => pct(r2.hit_ret_5m)],
    ["avg MFE", (r2) => num(r2.avg_mfe, 4)],
    ["avg MAE", (r2) => num(r2.avg_mae, 4)],
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
      for (const [sym, r] of Object.entries(msg.rows)) updateRow(sym, r, msg.ts);
      resortGrid();
      updateStatus(msg.status);
      if (msg.board) {
        renderBoard(msg.board.active);
        renderLearning(msg.board.learning);
      }
    } else if (msg.type === "entry") {
      onEntry(msg.trade);
    } else if (msg.type === "exit") {
      onExit(msg.trade);
    } else if (msg.type === "candles") {
      onCandles(msg);
    } else if (msg.type === "hello") {
      onHello(msg);
    }
    // "signal" events feed the DB/stats; the board is the visible surface
  };
  ws.onclose = () => {
    $("disconnect-banner").hidden = false;
    setTimeout(connect, state.wsRetry);
    state.wsRetry = Math.min(state.wsRetry * 2, 15000);
  };
}

function onHello(msg) {
  $("ver").textContent = msg.version ? "v" + msg.version : "";
  state.maxHold = msg.max_hold_s || 7200;
  state.alertScore = msg.alert_score;
  state.watchScore = msg.watch_score;
  if (msg.symbols.join(",") !== state.symbols.join(",")) {
    // fresh build OR the server restarted with a different watchlist:
    // rebuild so removed symbols don't linger as frozen ghost rows
    gridBody.textContent = "";
    state.rows.clear();
    lastOrder = "";
    state.symbols = msg.symbols;
    for (const sym of msg.symbols) buildRow(sym);
  }
  boardCards.querySelectorAll(".card").forEach((c) => c.remove());
  state.cards.clear();
  ticker.textContent = "";
  if (msg.board) {
    renderBoard(msg.board.active);
    for (const t of msg.board.resolved || []) ticker.appendChild(tickerLi(t, false));
    renderLearning(msg.board.learning);
  }
  const saved = localStorage.getItem("ts-sound");
  setSound(saved === null ? msg.sound_default : saved === "1", false);
}

connect();
