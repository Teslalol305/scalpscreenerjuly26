/* TapeScreen frontend v5: conviction-scaled signal cards (ring + trade gauge +
   entry ladder), labeled market grid, learning meters, help overlay.
   Vanilla JS, no build step. Gauge/ring positions are R-space, animated via CSS. */
"use strict";

const $ = (id) => document.getElementById(id);
const gridBody = $("grid-body");
const boardCards = $("board-cards");
const ticker = $("ticker");

const state = {
  symbols: [],
  rows: new Map(),
  cards: new Map(),       // trade id -> {el, refs}
  thoughts: [],           // reasoning trace, newest last (cap 400)
  thinkFilter: "all",
  thinkPaused: false,
  thinkPending: 0,
  tally: { win: 0, loss: 0 },
  soundOn: false,
  alertScore: 80,
  watchScore: 60,
  maxHold: 7200,
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
function fmtHold(s) {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}
function shortRule(r) {
  return r.replace("_ignition", "").replace("book_imbalance", "book imb")
    .replace("_", " ");
}

/* ---------------- market grid ---------------- */

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
  cells.cvd.innerHTML = `<canvas class="spark" width="92" height="24"></canvas>`;
  cells.imb.innerHTML = `<span class="imb-wrap"><span class="imb-bar"><i class="b"></i><i class="a"></i></span><span class="imb-txt"></span></span>`;
  cells.fund.innerHTML = `<span class="fund-badge"></span>`;
  cells.fundBadge = cells.fund.querySelector(".fund-badge");
  for (const c of ["sl", "ss"]) {
    cells[c].innerHTML = `<span class="scorecell"><b></b><span class="sc-bar"><i></i></span></span>`;
    cells[c + "Cell"] = cells[c].querySelector(".scorecell");
    cells[c + "Num"] = cells[c].querySelector("b");
    cells[c + "Bar"] = cells[c].querySelector(".sc-bar i");
  }
  tr.addEventListener("click", () => openDrawer(sym));
  gridBody.appendChild(tr);
  state.rows.set(sym, { tr, cells, spark: [], sparkTs: 0, lastPrice: 0 });
}

function flash(td, dir) {
  td.classList.remove("flash-up", "flash-dn");
  void td.offsetWidth;
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
  ctx.lineWidth = 1.6;
  ctx.stroke();
}

function scoreColor(score) {
  if (score >= state.alertScore) return "var(--dn)";
  if (score >= state.watchScore) return "var(--watch)";
  return "var(--accent)";
}

function updateRow(sym, r, now) {
  const row = state.rows.get(sym);
  if (!row) return;
  const { cells, tr } = row;
  tr.classList.toggle("stale", !!r.stale);
  tr.classList.toggle("qtn-row", !!r.quarantined);

  if (r.price !== row.lastPrice && row.lastPrice > 0) flash(cells.price, r.price - row.lastPrice);
  row.lastPrice = r.price;
  cells.price.textContent = fmtPrice(r.price);

  for (const [cell, v] of [[cells.pct1, r.pct_1m], [cells.pct5, r.pct_5m], [cells.pct15, r.pct_15m]]) {
    cell.textContent = fmtPct(v);
    cell.className = cell.className.replace(/ ?num-(up|dn)/g, "") + " " +
      pctClass(v / 100);
  }
  cells.volz.textContent = r.vol_z.toFixed(1);
  cells.volz.style.fontWeight = Math.abs(r.vol_z) >= 2.5 ? "800" : "400";
  cells.volz.style.color = Math.abs(r.vol_z) >= 2.5 ? "var(--watch)" : "";

  if (now - row.sparkTs >= 1) {
    row.sparkTs = now;
    row.spark.push(r.cvd_5m);
    if (row.spark.length > 300) row.spark.shift();
    drawSpark(cells.cvd.querySelector("canvas"), row.spark);
  }

  const bid = Math.round(r.imb * 62);
  const bar = cells.imb.querySelector(".imb-bar");
  bar.querySelector(".b").style.width = bid + "px";
  bar.querySelector(".a").style.width = (62 - bid) + "px";
  cells.imb.querySelector(".imb-txt").textContent = r.imb.toFixed(2);

  cells.spr.textContent = r.spread_bps.toFixed(1);
  const badge = cells.fundBadge;
  badge.textContent = `${fmtSigned(r.funding * 1e4, 2)}`;
  badge.className = `fund-badge ${r.funding_bias ? "hot" : ""}`;
  badge.title = `hourly funding · 7-day percentile ${r.funding_pctl}` +
    (r.funding_bias ? ` · crowded, fade bias: ${r.funding_bias}` : "");
  cells.doi.textContent = fmtSigned(r.doi_5m, Math.abs(r.doi_5m) >= 100 ? 0 : 2);

  for (const [k, score] of [["sl", r.score_long], ["ss", r.score_short]]) {
    const tier = score >= state.alertScore ? "alert" : score >= state.watchScore ? "watch" : "";
    cells[k + "Cell"].className = `scorecell ${tier}`;
    cells[k + "Num"].textContent = Math.round(score);
    const bar2 = cells[k + "Bar"];
    bar2.style.width = Math.min(100, score) + "%";
    bar2.style.background = scoreColor(score);
  }

  let badges = "";
  if (r.unavailable) badges += `<span class="badge badge-stale" title="not tradable on the venue">n/a</span>`;
  if (r.quarantined) badges += `<span class="badge badge-qtn" title="failing data self-audits — signals suspended">qtn</span>`;
  if (r.warming) badges += `<span class="badge badge-warm" title="statistics warming up (~30 min) — no signals yet">warm</span>`;
  if (r.stale && !r.unavailable) badges += `<span class="badge badge-stale" title="no trades for 30s">stale</span>`;
  if (r.oi_compression) badges += `<span class="badge badge-oi" title="open interest building while price sits still">OI</span>`;
  if (cells.badges.innerHTML !== badges) cells.badges.innerHTML = badges;
  row.sortKey = Math.max(r.score_long, r.score_short);
}

let lastOrder = "";
function resortGrid() {
  const rows = [...state.rows.values()].sort((a, b) => (b.sortKey || 0) - (a.sortKey || 0));
  const order = rows.map((r) => r.tr.dataset.sym).join(",");
  if (order === lastOrder) return;
  lastOrder = order;
  rows.forEach((r) => gridBody.appendChild(r.tr));
}

/* ---------------- signal cards ---------------- */

// gauge maps R to x%: display window is -1.5R .. +3R (entry sits at 33.3%)
function rToPct(r) {
  return Math.max(1, Math.min(99, ((r + 1.5) / 4.5) * 100));
}
function convTier(conf) { return conf >= 58 ? "conv-hi" : conf >= 48 ? "conv-mid" : "conv-lo"; }
function ringSize(tier) { return tier === "conv-hi" ? 58 : tier === "conv-mid" ? 48 : 40; }

function buildCard(t, fresh) {
  const tier = convTier(t.confidence);
  const size = ringSize(tier);
  const r = size / 2 - 4, C = (2 * Math.PI * r).toFixed(2);
  const el = document.createElement("div");
  el.className = `card ${t.side} ${tier} ${fresh ? "enter" : ""}`;
  el.dataset.id = t.id;
  const src = t.conf_src === "model" ? "ml model" : "history";
  el.innerHTML = `
    <div class="r1">
      <span class="side-pill ${t.side}">${t.side.toUpperCase()}</span>
      <span class="sym">${t.symbol}</span>
      <span class="tierchip ${t.tier}">${t.tier}</span>
      <span class="rule">${shortRule(t.rule)}</span>
      <span class="conf">
        <span class="conf-label"><b>WIN PROBABILITY</b><span>${src} · n=${t.conf_n}</span></span>
        <span class="conf-wrap" style="width:${size}px;height:${size}px">
          <svg class="conf-ring" width="${size}" height="${size}">
            <circle class="bgc" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke-width="4"/>
            <circle class="fgc" cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke-width="4"
              stroke-dasharray="${C}" stroke-dashoffset="${C}"/>
          </svg><span class="conf-num">${Math.round(t.confidence)}%</span>
        </span>
      </span>
    </div>
    <div class="body">
      <div class="ladder">
        <span class="lad-t">ENTRY LADDER</span>
        ${(t.levels || []).map((lv, i) =>
          `<span class="lvl" data-i="${i}" title="scale-in level ${i + 1}"><i></i>E${i + 1} <b>${fmtPrice(lv.px)}</b></span>`).join("")}
      </div>
      <div class="gauge-zone">
        <div class="gauge-top">
          <span class="lad-t">TRADE GAUGE</span>
          <span class="statechip"></span>
          <span class="bankchip" title="profit already secured by the +1R partial"></span>
          <span class="liveR" title="total open result, in R units" style="margin-left:auto"></span>
        </div>
        <div class="gauge">
          <div class="track"></div>
          <div class="mk mk-stop" title="stop"></div>
          <div class="mk mk-entry" title="average entry"></div>
          <div class="mk mk-price" title="live price"></div>
        </div>
        <div class="gauge-labels">
          <span>stop <b class="g-stop"></b></span>
          <span>entry <b class="g-entry"></b></span>
          <span>+3R</span>
        </div>
      </div>
    </div>
    <div class="age">
      <span class="age-bar"><i></i></span><span class="age-txt"></span>
    </div>`;
  el.addEventListener("click", () => openDrawer(t.symbol));
  const refs = {
    fgc: el.querySelector(".fgc"), C: parseFloat(C),
    num: el.querySelector(".conf-num"),
    lvls: [...el.querySelectorAll(".lvl")],
    statechip: el.querySelector(".statechip"),
    bankchip: el.querySelector(".bankchip"),
    liveR: el.querySelector(".liveR"),
    mkStop: el.querySelector(".mk-stop"),
    mkPrice: el.querySelector(".mk-price"),
    gStop: el.querySelector(".g-stop"),
    gEntry: el.querySelector(".g-entry"),
    ageBar: el.querySelector(".age-bar i"),
    ageTxt: el.querySelector(".age-txt"),
  };
  requestAnimationFrame(() => {  // let the ring animate from empty
    refs.fgc.style.strokeDashoffset = (refs.C * (1 - t.confidence / 100)).toFixed(2);
  });
  return { el, refs };
}

function updateCard(entry, t) {
  const { refs } = entry;
  (t.levels || []).forEach((lv, i) => {
    const el = refs.lvls[i];
    if (el) el.classList.toggle("filled", !!lv.filled);
  });
  refs.gStop.textContent = fmtPrice(t.stop);
  refs.gEntry.textContent = fmtPrice(t.avg_entry);
  refs.statechip.textContent = t.state === "INIT" ? "" : t.state;
  refs.statechip.className = "statechip st-" + t.state;
  refs.statechip.title = t.state === "BE" ? "stop moved to break-even"
    : t.state === "TRAIL" ? "trailing stop active, 1R behind best price" : "";
  refs.bankchip.textContent = t.tp1_done ? `banked +${t.realized}R` : "";
  refs.liveR.textContent = fmtSigned(t.live_r, 2) + "R";
  refs.liveR.className = "liveR " +
    (t.live_r > 0.05 ? "num-up" : t.live_r < -0.05 ? "num-dn" : "");

  const legR = t.leg_r ?? 0, stopR = t.stop_r ?? -1;
  refs.mkStop.style.left = rToPct(stopR) + "%";
  refs.mkPrice.style.left = rToPct(legR) + "%";
  refs.mkPrice.className = "mk mk-price " + (legR > 0.05 ? "pos" : legR < -0.05 ? "neg" : "");

  const age = Date.now() / 1000 - t.ts;
  refs.ageBar.style.width = Math.min(100, age / state.maxHold * 100).toFixed(1) + "%";
  refs.ageTxt.textContent = `${fmtHold(age)} / ${fmtHold(state.maxHold)}`;
}

function renderBoard(active) {
  const seen = new Set();
  for (const t of active) {
    seen.add(String(t.id));
    let entry = state.cards.get(String(t.id));
    if (!entry) {
      entry = buildCard(t, false);
      state.cards.set(String(t.id), entry);
      boardCards.appendChild(entry.el);
    }
    updateCard(entry, t);
  }
  for (const [id, entry] of state.cards) {
    if (!seen.has(id)) { entry.el.remove(); state.cards.delete(id); }
  }
  $("board-empty").style.display = state.cards.size ? "none" : "block";
  $("board-count").textContent = state.cards.size ? `${state.cards.size} open` : "";
}

function onEntry(t) {
  if (!state.cards.has(String(t.id))) {
    const entry = buildCard(t, true);
    state.cards.set(String(t.id), entry);
    boardCards.prepend(entry.el);
    updateCard(entry, t);
    $("board-empty").style.display = "none";
    $("board-count").textContent = `${state.cards.size} open`;
  }
  if (state.soundOn) blip(660, 0.09);
  if (t.tier === "ALERT") alertUser(t);
}

function tickerLi(t, fresh) {
  const li = document.createElement("li");
  if (fresh) li.className = "fresh";
  const win = t.status === "win";
  const why = { STOP: "stopped out", BE: "break-even stop", TRAIL: "trailing stop", TIME: "2h time limit" }[t.exit_reason] || t.exit_reason;
  li.innerHTML = `
    <span class="res-pill ${t.status}">${win ? "WIN" : "LOSS"}</span>
    <span class="r-val ${win ? "num-up" : "num-dn"}">${fmtSigned(t.r_result, 2)}R</span>
    <span class="sym">${t.symbol}</span>
    <span class="side-${t.side}">${t.side}</span>
    <span class="why">${shortRule(t.rule)} · ${why}</span>
    <span class="t">${fmtClock(t.exit_ts)}</span>`;
  return li;
}

function onExit(t) {
  const entry = state.cards.get(String(t.id));
  if (entry) { entry.el.remove(); state.cards.delete(String(t.id)); }
  ticker.prepend(tickerLi(t, true));
  while (ticker.children.length > 40) ticker.lastChild.remove();
  state.tally[t.status] = (state.tally[t.status] || 0) + 1;
  const { win = 0, loss = 0 } = state.tally;
  $("ticker-tally").textContent = `W ${win} · L ${loss}`;
  if (state.soundOn) blip(t.status === "win" ? 880 : 330, 0.12);
  $("board-empty").style.display = state.cards.size ? "none" : "block";
  $("board-count").textContent = state.cards.size ? `${state.cards.size} open` : "";
}

function renderLearning(learning) {
  const box = $("learn-meters");
  if (!learning?.length) {
    box.innerHTML = "<span class='note'>no resolved signals yet — every strategy starts at 50% and must earn its confidence</span>";
    return;
  }
  box.innerHTML = learning.map((l) => {
    const wr = l.win_rate;
    const col = wr == null ? "var(--ink-3)" : wr >= 55 ? "var(--up)" : wr >= 45 ? "var(--accent)" : "var(--dn)";
    return `
    <div class="meter" title="${l.n} resolved signals · avg ${l.avg_r ?? "?"}R per signal · score weight ×${l.weight_mult} · ML trained on ${l.model_n}">
      <div class="m-top"><span class="m-name">${shortRule(l.rule)}</span>
        <span class="m-val" style="color:${col}">${wr == null ? "–" : wr + "%"}</span></div>
      <div class="m-bar"><i style="width:${wr || 0}%;background:${col}"></i></div>
      <div class="m-sub">n=${l.n}${l.avg_r == null ? "" : ` · ${l.avg_r > 0 ? "+" : ""}${l.avg_r}R avg`} · w×${l.weight_mult} · ml:${l.model_n}</div>
    </div>`;
  }).join("");
}

/* ---------------- reasoning feed ---------------- */

const thinkFeed = $("think-feed");
const THINK_DOM_CAP = 120;

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function thoughtLi(th, fresh) {
  const li = document.createElement("li");
  li.className = `th cat-${th.cat}${fresh ? " fresh" : ""}`;
  li.innerHTML = `
    <div class="th-top">
      <span class="th-time">${fmtClock(th.ts)}</span>
      <span class="th-cat">${th.cat}</span>
      ${th.symbol ? `<span class="th-sym">${esc(th.symbol)}</span>` : ""}
      <span class="th-head">${esc(th.headline)}</span>
    </div>
    ${th.detail?.length
      ? `<div class="th-detail">${th.detail.map((d) => `<div>${esc(d)}</div>`).join("")}</div>`
      : ""}`;
  if (th.symbol) li.addEventListener("click", () => openDrawer(th.symbol));
  return li;
}

function thinkMatches(th) {
  return state.thinkFilter === "all" || th.cat === state.thinkFilter ||
    (state.thinkFilter === "audit" && th.cat === "system");
}

function renderThinkFeed() {
  thinkFeed.textContent = "";
  const shown = state.thoughts.filter(thinkMatches).slice(-THINK_DOM_CAP);
  for (let i = shown.length - 1; i >= 0; i--) thinkFeed.appendChild(thoughtLi(shown[i], false));
  if (!thinkFeed.children.length) {
    const li = document.createElement("li");
    li.id = "think-empty";
    li.textContent = "no decisions in this category yet — they appear here the moment the system makes one.";
    thinkFeed.appendChild(li);
  }
}

function onThought(th) {
  state.thoughts.push(th);
  if (state.thoughts.length > 400) state.thoughts.shift();
  if (state.thinkPaused) { state.thinkPending++; return; }
  if (!thinkMatches(th)) return;
  const empty = $("think-empty");
  if (empty) empty.remove();
  thinkFeed.prepend(thoughtLi(th, true));
  while (thinkFeed.children.length > THINK_DOM_CAP) thinkFeed.lastChild.remove();
}

thinkFeed.addEventListener("mouseenter", () => {
  state.thinkPaused = true;
  $("think-paused").hidden = false;
});
thinkFeed.addEventListener("mouseleave", () => {
  state.thinkPaused = false;
  $("think-paused").hidden = true;
  if (state.thinkPending) { state.thinkPending = 0; renderThinkFeed(); }
});

document.querySelectorAll(".tf-chip").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tf-chip").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  state.thinkFilter = b.dataset.cat;
  state.thinkPending = 0;
  renderThinkFeed();
}));

function setThinkCollapsed(collapsed, save) {
  $("think-pane").classList.toggle("collapsed", collapsed);
  $("think-collapse").textContent = collapsed ? "▴" : "▾";
  if (save) localStorage.setItem("ts-think", collapsed ? "1" : "0");
}
$("think-collapse").addEventListener("click", () =>
  setThinkCollapsed(!$("think-pane").classList.contains("collapsed"), true));
setThinkCollapsed(localStorage.getItem("ts-think") === "1", false);

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

  const au = $("st-audit"), a = st.audit, b = au.querySelector("b");
  if (!a) { b.textContent = "–"; au.title = "self-audit has not run yet"; }
  else if (a.ok) {
    b.textContent = "✓";
    b.style.color = "var(--up)";
    au.title = `self-audit clean · run #${a.runs} · data cross-checks + logic invariants`;
  } else {
    b.textContent = a.failures.length + "!";
    b.style.color = "var(--dn)";
    au.title = "SELF-AUDIT ISSUES:\n" + a.failures.join("\n") +
      (a.quarantined.length ? "\nquarantined: " + a.quarantined.join(", ") : "");
  }
}

/* ---------------- sounds & alerts ---------------- */

let audioCtx = null;
function actx() {
  audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === "suspended") audioCtx.resume();
  return audioCtx;
}
function blip(freq, dur) {
  const ac = actx(), t0 = ac.currentTime;
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
      body: `${t.rule} · win prob ${Math.round(t.confidence)}% @ ${fmtPrice(t.avg_entry || t.entry)}`,
      tag: `ts-${t.symbol}-${t.side}`,
    });
  }
}

function setSound(on, interactive) {
  state.soundOn = on;
  $("sound-toggle").textContent = on ? "🔔" : "🔇";
  $("sound-toggle").classList.toggle("on", on);
  if (on && interactive) {
    beep();
    if (Notification.permission === "default") Notification.requestPermission();
  }
}
$("sound-toggle").addEventListener("click", () => {
  const on = !state.soundOn;
  localStorage.setItem("ts-sound", on ? "1" : "0");
  setSound(on, true);
});

/* ---------------- help overlay ---------------- */

$("help-toggle").addEventListener("click", () => { $("help-overlay").hidden = false; });
$("help-close").addEventListener("click", () => { $("help-overlay").hidden = true; });
$("help-overlay").addEventListener("click", (e) => {
  if (e.target === $("help-overlay")) $("help-overlay").hidden = true;
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("help-overlay").hidden = true; closeDrawer(); }
});

/* ---------------- drawer (lightweight-charts) ---------------- */

const CHART_OPTS = {
  autoSize: true,
  layout: { background: { color: "#11151d" }, textColor: "#9aa4b8", fontSize: 11 },
  grid: { vertLines: { color: "#171c26" }, horzLines: { color: "#171c26" } },
  timeScale: { timeVisible: true, secondsVisible: true, borderColor: "#222938" },
  rightPriceScale: { borderColor: "#222938" },
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
    ["n", (x) => x.signals],
    ["alerts", (x) => x.alerts ?? 0],
    ["hit 30s", (x) => pct(x.hit_ret_30s)],
    ["hit 1m", (x) => pct(x.hit_ret_1m)],
    ["hit 3m", (x) => pct(x.hit_ret_3m)],
    ["hit 5m", (x) => pct(x.hit_ret_5m)],
    ["avg MFE", (x) => num(x.avg_mfe, 4)],
    ["avg MAE", (x) => num(x.avg_mae, 4)],
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
    } else if (msg.type === "thought") {
      onThought(msg.thought);
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
  $("ver").textContent = msg.version ? "v" + msg.version : "";
  state.maxHold = msg.max_hold_s || 7200;
  state.alertScore = msg.alert_score;
  state.watchScore = msg.watch_score;
  if (msg.symbols.join(",") !== state.symbols.join(",")) {
    gridBody.textContent = "";
    state.rows.clear();
    lastOrder = "";
    state.symbols = msg.symbols;
    for (const sym of msg.symbols) buildRow(sym);
  }
  boardCards.querySelectorAll(".card").forEach((c) => c.remove());
  state.cards.clear();
  ticker.textContent = "";
  state.thoughts = msg.thoughts || [];
  state.thinkPending = 0;
  renderThinkFeed();
  if (msg.board) {
    renderBoard(msg.board.active);
    for (const t of msg.board.resolved || []) ticker.appendChild(tickerLi(t, false));
    renderLearning(msg.board.learning);
  }
  const saved = localStorage.getItem("ts-sound");
  setSound(saved === null ? msg.sound_default : saved === "1", false);
}

connect();
