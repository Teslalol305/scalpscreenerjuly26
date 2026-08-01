/* TapeScreen frontend v7: card workspace (drag/resize/hide, layout persisted),
   session performance hero + equity curve, quant-desk & tracked-variables cards,
   conviction-scaled signal cards, labeled market grid, live reasoning feed.
   Vanilla JS, no build step. */
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
  lastCurve: [],
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
function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------------- card workspace: drag / resize / hide, persisted ---------------- */

const LAYOUT_KEY = "ts-layout-v7";
const GRID_GAP = 12, GRID_COLS = 12, GRID_ROW = 50;
let draggingCard = null;

function cardEls() { return [...document.querySelectorAll("#cards .dcard")]; }
function spanOf(v, fb) { const m = /span (\d+)/.exec(v || ""); return m ? +m[1] : fb; }

function saveLayout() {
  const out = { order: [], cards: {} };
  for (const el of cardEls()) {
    const id = el.dataset.card;
    out.order.push(id);
    out.cards[id] = {
      w: spanOf(el.style.gridColumn, +el.dataset.w),
      h: spanOf(el.style.gridRow, +el.dataset.h),
      hid: el.hidden,
    };
  }
  localStorage.setItem(LAYOUT_KEY, JSON.stringify(out));
}

function applyCard(el, cfg) {
  el.style.gridColumn = `span ${Math.max(3, Math.min(12, cfg.w))}`;
  el.style.gridRow = `span ${Math.max(3, Math.min(24, cfg.h))}`;
  el.hidden = !!cfg.hid;
}

function initLayout() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(LAYOUT_KEY)); } catch { /* fresh */ }
  const grid = $("cards");
  if (saved?.order) {
    for (const id of saved.order) {
      const el = grid.querySelector(`[data-card="${id}"]`);
      if (el) grid.appendChild(el);
    }
  }
  const toggles = $("card-toggles");
  for (const el of cardEls()) {
    const id = el.dataset.card;
    const cfg = saved?.cards?.[id] || { w: +el.dataset.w, h: +el.dataset.h, hid: false };
    applyCard(el, cfg);

    // corner resize handle
    const rh = document.createElement("div");
    rh.className = "rs-handle";
    rh.title = "drag to resize";
    el.appendChild(rh);
    rh.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      const cellW = (grid.clientWidth - GRID_GAP * (GRID_COLS - 1)) / GRID_COLS;
      const w0 = spanOf(el.style.gridColumn, +el.dataset.w);
      const h0 = spanOf(el.style.gridRow, +el.dataset.h);
      const x0 = e.clientX, y0 = e.clientY;
      el.classList.add("resizing");
      const move = (ev) => {
        const w = w0 + Math.round((ev.clientX - x0) / (cellW + GRID_GAP));
        const h = h0 + Math.round((ev.clientY - y0) / (GRID_ROW + GRID_GAP));
        applyCard(el, { w, h, hid: false });
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        el.classList.remove("resizing");
        saveLayout();
        drawEquity(state.lastCurve);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });

    // drag-to-reorder via the ⠿ handle
    const grab = el.querySelector(".grab");
    grab.addEventListener("mousedown", () => el.setAttribute("draggable", "true"));
    el.addEventListener("dragstart", (e) => {
      draggingCard = el;
      el.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
      try { e.dataTransfer.setData("text/plain", id); } catch { /* IE */ }
    });
    el.addEventListener("dragend", () => {
      el.classList.remove("dragging");
      el.removeAttribute("draggable");
      draggingCard = null;
      saveLayout();
      drawEquity(state.lastCurve);
    });

    // hide button + sidebar toggle
    const title = el.querySelector(".ct h3").textContent;
    const lab = document.createElement("label");
    lab.className = "ctog";
    lab.innerHTML = `<input type="checkbox" ${cfg.hid ? "" : "checked"}> ${esc(title)}`;
    const cb = lab.querySelector("input");
    cb.addEventListener("change", () => {
      el.hidden = !cb.checked;
      saveLayout();
      drawEquity(state.lastCurve);
    });
    toggles.appendChild(lab);
    el.querySelector(".hide-btn").addEventListener("click", () => {
      el.hidden = true;
      cb.checked = false;
      saveLayout();
    });
  }
  grid.addEventListener("dragover", (e) => {
    if (!draggingCard) return;
    e.preventDefault();
    const t = e.target.closest(".dcard");
    if (!t || t === draggingCard) return;
    const r = t.getBoundingClientRect();
    const before = e.clientY < r.top + r.height / 2;
    grid.insertBefore(draggingCard, before ? t : t.nextSibling);
  });
  $("layout-reset").addEventListener("click", () => {
    localStorage.removeItem(LAYOUT_KEY);
    location.reload();
  });
}

/* ---------------- hero + equity curve ---------------- */

function renderHero(h) {
  if (!h) return;
  const net = $("h-netr");
  net.textContent = (h.net_r > 0 ? "+" : "") + h.net_r + "R";
  net.style.color = h.net_r > 0.005 ? "var(--up)" : h.net_r < -0.005 ? "var(--dn)" : "";
  $("h-wr").textContent = h.win_rate == null ? "–" : h.win_rate + "%";
  $("h-exp").textContent = h.expectancy == null ? "–" : fmtSigned(h.expectancy) + "R";
  $("h-trades").textContent = h.trades;
  $("h-open").textContent = h.open;
}

function drawEquity(curve) {
  state.lastCurve = curve || [];
  const wrap = $("equity-wrap"), cv = $("equity");
  const W = wrap.clientWidth, H = wrap.clientHeight;
  $("equity-empty").style.display = curve?.length ? "none" : "flex";
  if (!W || !H) return;
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.round(W * dpr);
  cv.height = Math.round(H * dpr);
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!curve?.length) return;

  let cum = 0;
  const pts = curve.map((p) => [p[0], (cum += p[1])]);
  pts.unshift([pts[0][0] - 1, 0]);  // rebase: window starts flat at 0
  const padL = 6, padR = 46, padY = 10;
  const t0 = pts[0][0], t1 = pts[pts.length - 1][0] || t0 + 1;
  const ys = pts.map((p) => p[1]);
  const yMin = Math.min(0, ...ys), yMax = Math.max(0, ...ys);
  const ySpan = (yMax - yMin) || 1;
  const X = (t) => padL + ((t - t0) / Math.max(1e-9, t1 - t0)) * (W - padL - padR);
  const Y = (v) => padY + (1 - (v - yMin) / ySpan) * (H - 2 * padY);

  // zero line
  ctx.setLineDash([3, 4]);
  ctx.strokeStyle = "rgba(156,156,184,.35)";
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(padL, Y(0)); ctx.lineTo(W - padR, Y(0)); ctx.stroke();
  ctx.setLineDash([]);

  // area fill to zero
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, "rgba(167,139,250,.28)");
  grad.addColorStop(1, "rgba(167,139,250,.02)");
  ctx.beginPath();
  ctx.moveTo(X(pts[0][0]), Y(0));
  for (const [t, v] of pts) ctx.lineTo(X(t), Y(v));
  ctx.lineTo(X(t1), Y(0));
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // the line (step-after reads truthfully for discrete trade outcomes)
  ctx.beginPath();
  pts.forEach(([t, v], i) => {
    const x = X(t), y = Y(v);
    if (i === 0) ctx.moveTo(x, y);
    else { ctx.lineTo(x, Y(pts[i - 1][1])); ctx.lineTo(x, y); }
  });
  ctx.strokeStyle = "#a78bfa";
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  ctx.shadowColor = "rgba(139,92,246,.55)";
  ctx.shadowBlur = 8;
  ctx.stroke();
  ctx.shadowBlur = 0;

  // end dot + value
  const last = pts[pts.length - 1];
  ctx.beginPath();
  ctx.arc(X(last[0]), Y(last[1]), 3.5, 0, Math.PI * 2);
  ctx.fillStyle = "#c4b5fd";
  ctx.fill();
  ctx.font = "700 11px ui-monospace, Menlo, monospace";
  ctx.fillStyle = last[1] >= 0 ? "#26a69a" : "#ef5350";
  ctx.fillText((last[1] > 0 ? "+" : "") + last[1].toFixed(2) + "R",
    W - padR + 6, Y(last[1]) + 4);
}
new ResizeObserver(() => drawEquity(state.lastCurve)).observe($("equity-wrap"));

/* ---------------- quant desk + tracked variables ---------------- */

function renderDesk(d) {
  const box = $("desk-body");
  if (!d) {
    box.innerHTML = "<p class='empty-note'>research desk disabled in config</p>";
    return;
  }
  const probs = Object.entries(d.probation || {});
  let h = `<div class="desk-meta"><b>${d.meetings}</b><u>meetings held</u>` +
    (d.last_ts ? `<u>· last ${fmtClock(d.last_ts)}</u>` : "") + `</div>`;
  h += `<div class="desk-sec"><h5>findings</h5>` +
    ((d.findings?.length)
      ? d.findings.map((f) => `<div class="desk-finding">${esc(f)}</div>`).join("")
      : `<p class="desk-ok">the desk convenes after new outcomes resolve — findings appear here</p>`)
    + `</div>`;
  h += `<div class="desk-sec"><h5>probation</h5>`;
  if (probs.length) {
    h += probs.map(([rule, p]) =>
      `<div class="prob-chip"><b>${esc(shortRule(rule))}</b>
       <span>needs ≥${Math.round(p.conf_min * 100)}% · held ${p.suppressed} ·
       explored ${p.explored} (1 in ${p.every} opens)</span></div>`).join("");
  } else {
    h += `<p class="desk-ok">none — every strategy is trading on its own record</p>`;
  }
  h += `</div>`;
  if (d.next_focus) {
    h += `<div class="desk-sec"><h5>next focus</h5><p class="desk-focus">${esc(d.next_focus)}</p></div>`;
  }
  box.innerHTML = h;
}

function varRow(name, r, sub, discovered) {
  const width = Math.min(100, Math.abs(r || 0) * 250).toFixed(0);
  return `<div class="var-row">
    <div class="vr-top"><span class="vr-name ${discovered ? "discovered" : ""}">${esc(name)}</span>
      <span class="vr-r">${r == null ? "" : "r " + fmtSigned(r, 3)}</span></div>
    <div class="vr-bar"><i style="width:${width}%"></i></div>
    <div class="vr-sub">${esc(sub)}</div></div>`;
}

function renderVars(d) {
  const box = $("vars-body");
  if (!d) {
    box.innerHTML = "<p class='empty-note'>research desk disabled in config</p>";
    return;
  }
  const nx = (d.active_extras || []).length;
  let h = `<div class="vars-head">model inputs: <b>${d.base_features} base + ${nx} discovered</b></div>`;
  for (const v of d.active_extras || []) {
    h += varRow(v.name, v.live_r ?? v.r,
      `promoted at r ${fmtSigned(v.r ?? 0, 3)} over ${v.n ?? "?"} trades — now in every model`,
      true);
  }
  const under = Object.entries(d.candidates || {})
    .filter(([n]) => !(d.active_extras || []).some((v) => v.name === n))
    .sort((a, b) => Math.abs(b[1].r) - Math.abs(a[1].r)).slice(0, 4);
  if (under.length) {
    h += `<div class="vars-head" style="margin-top:10px">under study</div>`;
    for (const [n, s] of under) {
      h += varRow(n, s.r, `${s.n} trades measured — promotion needs |r| ≥ 0.15`, false);
    }
  } else if (!nx) {
    h += `<p class="vars-note">the scout measures ${11} candidate variables (time of day,
      funding percentile, squeeze regime, OI thrust, book persistence…) on every trade.
      Once 40 resolve, anything with real predictive correlation is promoted into the
      models — the system discovers its own new data points.</p>`;
  }
  box.innerHTML = h;
}

/* ---------------- validation tracker ---------------- */

function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function valBar(label, cur, target) {
  const pct = Math.min(100, (cur / target) * 100).toFixed(0);
  return `<div class="vt-bar"><u>${esc(label)}</u>
    <span class="vt-track"><i style="width:${pct}%"></i></span>
    <b>${cur}/${target}</b></div>`;
}

function renderValidation(v) {
  const box = $("validate-body");
  if (!v) return;
  const t = v.targets;
  const nOk = v.validated.length;
  let h = `<div class="val-banner ${nOk ? "ok" : ""}">${nOk
    ? `✓ ${v.validated.map(shortRule).join(", ")} validated for real-trading consideration`
    : "no strategy validated yet — do not trade real money on these signals"}</div>`;
  h += `<div class="val-crit">the bar, per strategy: ≥${t.min_trades} R-trades · ≥${t.min_days} active days · avg R confidently &gt; 0 (95%, net of spread + fees) · off probation</div>`;
  for (const r of v.rules) {
    const ciTxt = r.mean_r == null ? ""
      : `${fmtSigned(r.mean_r)}R <span class="vt-ci">± ${r.ci_lo == null ? "?" : (r.mean_r - r.ci_lo).toFixed(2)}</span>`;
    const ciCls = r.ci_lo != null && r.ci_lo > 0 ? "num-up" : r.ci_hi != null && r.ci_hi < 0 ? "num-dn" : "";
    let eta;
    if (r.status === "validated") eta = "record complete — gates cleared";
    else if (r.status === "rejected") eta = "verdict: no measurable edge — do not trade this strategy";
    else if (r.status === "waiting") eta = "waiting for its first resolved trades";
    else if (r.eta_ts) eta = `projected ready ~${fmtDate(r.eta_ts)} (${r.rate_per_day}/day)` +
      (r.on_probation ? " · must also clear probation" : "");
    else eta = "no recent trades — keep the app running 24/7";
    h += `<div class="vrow">
      <div class="vrow-top">
        <b class="vrow-name">${esc(shortRule(r.rule))}</b>
        <span class="vst vst-${r.status}">${r.status.toUpperCase()}</span>
        ${r.win_rate != null ? `<span class="vt-wr">${r.win_rate}% wr</span>` : ""}
        <span class="vt-exp ${ciCls}">${ciTxt}</span>
      </div>
      ${valBar("trades", r.n, t.min_trades)}
      ${valBar("days", r.days, t.min_days)}
      <div class="vt-eta">${esc(eta)}</div>
    </div>`;
  }
  box.innerHTML = h;
}

function renderHealth(st) {
  const a = st.audit;
  const fh = st.feed_health || {};
  const tiles = [
    a ? [a.ok ? "✓ clean" : a.failures.length + " issue(s)",
         `self-audit · run #${a.runs}`, a.ok ? "ok" : "bad"]
      : ["–", "self-audit · not run yet", ""],
    [(a?.quarantined?.length ? a.quarantined.join(" ") : "none"),
     "quarantined markets", a?.quarantined?.length ? "bad" : "ok"],
    [st.msg_rate ?? 0, "messages / s", ""],
    [(fh.dropped_msgs || 0) + (fh.queue_drops || 0), "dropped events",
     ((fh.dropped_msgs || 0) + (fh.queue_drops || 0)) ? "bad" : "ok"],
    [st.db_write_errors ?? 0, "db write errors", st.db_write_errors ? "bad" : "ok"],
    [st.pipeline_latency_p95_ms ? st.pipeline_latency_p95_ms + "ms" : "–",
     "tick→screen p95", ""],
    [st.outcomes_completed ?? 0, "outcomes measured", ""],
  ];
  $("health-body").innerHTML = tiles.map(([v, k, cls]) =>
    `<div class="hl-tile ${cls}"><div class="v">${esc(String(v))}</div><div class="k">${esc(k)}</div></div>`).join("");
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
      ${t.exploration ? `<span class="expchip" title="opened through the probation exploration quota — the strategy is gated but must keep earning evidence">explore</span>` : ""}
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

function boardCount() {
  $("board-count").textContent = state.cards.size
    ? `${state.cards.size} open`
    : "bigger & greener = higher measured win probability";
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
  boardCount();
}

function onEntry(t) {
  if (!state.cards.has(String(t.id))) {
    const entry = buildCard(t, true);
    state.cards.set(String(t.id), entry);
    boardCards.prepend(entry.el);
    updateCard(entry, t);
    $("board-empty").style.display = "none";
    boardCount();
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
  boardCount();
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

/* ---------------- sidebar status ---------------- */

function updateStatus(st) {
  const fh = st.feed_health || {};
  const connected = fh.connected !== undefined ? fh.connected : true;
  $("st-conn").querySelector(".dot").className = "dot " + (connected ? "ok" : "bad");
  $("st-venue").textContent = fh.venue || "replay";
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
  layout: { background: { color: "#12121b" }, textColor: "#9c9cb8", fontSize: 11 },
  grid: { vertLines: { color: "#181826" }, horzLines: { color: "#181826" } },
  timeScale: { timeVisible: true, secondsVisible: true, borderColor: "#232336" },
  rightPriceScale: { borderColor: "#232336" },
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
        price, title, color: "#a78bfa", lineStyle: style, lineWidth: 1, axisLabelVisible: false,
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

/* ---------------- stats view ---------------- */

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

document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  const stats = b.dataset.view === "stats";
  $("view-dash").hidden = stats;
  $("view-stats").hidden = !stats;
  if (stats) loadStats();
  else drawEquity(state.lastCurve);
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
      renderHealth(msg.status);
      if (msg.board) {
        renderBoard(msg.board.active);
        renderLearning(msg.board.learning);
      }
      renderHero(msg.hero);
      drawEquity(msg.curve || []);
      renderDesk(msg.desk);
      renderVars(msg.desk);
      renderValidation(msg.validation);
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
  renderHero(msg.hero);
  drawEquity(msg.curve || []);
  renderDesk(msg.desk);
  renderVars(msg.desk);
  renderValidation(msg.validation);
  const saved = localStorage.getItem("ts-sound");
  setSound(saved === null ? msg.sound_default : saved === "1", false);
}

initLayout();
connect();
