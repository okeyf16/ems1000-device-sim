// EMS-1000 web panel. Polls the simulator's local JSON endpoints; actions are POSTs
// with the X-EMS-Panel header (other websites can't send it).
"use strict";

const $ = (id) => document.getElementById(id);
const SVGNS = "http://www.w3.org/2000/svg";
let live = null;
let historyPoints = [];
let lastHistoryTs = 0;
let offline = false;

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-EMS-Panel": "1" },
    body: JSON.stringify(body),
  };
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 401 && data.login) {   // shared panel: session missing or expired
    window.location.replace(data.login);
    throw new Error("login required");
  }
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function el(tag, attrs = {}, text) {
  const node = attrs.svg ? document.createElementNS(SVGNS, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (k !== "svg") node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---- times ----------------------------------------------------------------------------
// The generators are in Nigeria, so the panel shows West Africa Time (UTC+1) everywhere.
const WAT_TIME = new Intl.DateTimeFormat("en-GB", { timeZone: "Africa/Lagos", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const WAT_DAY_TIME = new Intl.DateTimeFormat("en-GB", { timeZone: "Africa/Lagos", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const wat = (ms) => `${WAT_TIME.format(new Date(ms))} WAT`;
const watDay = (ms) => `${WAT_DAY_TIME.format(new Date(ms))} WAT`;

// ---- dials, strips and the oil lamp ------------------------------------------------------
// Each gauge: range, unit, and coloured bands from the simulator's own limits.
const loadMax = (L) => Math.round((L.max_a / L.rated_a) * 100);
const DIALS = [
  { key: "v", name: "Voltage", unit: "V", min: 0, max: 260, digits: 1,
    bands: (L) => [[L.nominal_v * (1 - L.v_tolerance_pct / 100), L.nominal_v * (1 + L.v_tolerance_pct / 100), "ok"]] },
  { key: "load_pct", name: "Load", unit: "%", min: 0, max: 150, digits: 0,
    bands: (L) => [[0, 100, "ok"], [100, loadMax(L), "warn"], [loadMax(L), 150, "bad"]] },
  { key: "fuel", name: "Fuel", unit: "%", min: 0, max: 100, digits: 1,
    bands: (L) => [[0, L.low_fuel_interlock_pct, "bad"], [L.low_fuel_interlock_pct, L.low_fuel_warn_pct, "warn"],
                   [L.low_fuel_warn_pct, 100, "ok"]] },
  { key: "t", name: "Engine temperature", unit: "°C", min: 0, max: 120, digits: 1,
    bands: (L) => [[0, L.restart_temp_c, "ok"], [L.restart_temp_c, L.overtemp_c, "warn"], [L.overtemp_c, 120, "bad"]] },
  { key: "soc_pct", name: "Battery charge", unit: "%", min: 0, max: 100, digits: 0,
    bands: () => [[0, 25, "bad"], [25, 50, "warn"], [50, 100, "ok"]] },
];
const STRIPS = [
  { key: "f", name: "Frequency", unit: "Hz", min: 40, max: 56, digits: 2,
    bands: (L) => [[L.f_low, L.f_high, "ok"]] },
  { key: "i", name: "Load current", unit: "A", min: 0, max: 20, digits: 1,
    bands: (L) => [[0, L.rated_a, "ok"], [L.rated_a, L.max_a, "warn"], [L.max_a, 20, "bad"]] },
];
const ALWAYS_BANDED = ["fuel", "t", "soc_pct"];   // the rest only mean something while running
const BAND_COLOUR = { ok: "#7fbf95", warn: "#e2b54f", bad: "#d9776f" };
const OIL_LAMP = [["ok", "Oil OK"], ["warn", "Oil low"], ["bad", "Oil critical"]];
const dialEls = {};
const stripEls = {};
let oilEl = null;
let lastRestSoc = null;   // battery charge measured with the engine off

function angle(d, value) {  // -120deg .. +120deg
  const f = Math.max(0, Math.min(1, (value - d.min) / (d.max - d.min)));
  return -120 + f * 240;
}
function polar(a, r) {
  const rad = (a - 90) * Math.PI / 180;
  return [100 + r * Math.cos(rad), 100 + r * Math.sin(rad)];
}
function arc(a0, a1, r) {
  const [x0, y0] = polar(a0, r), [x1, y1] = polar(a1, r);
  return `M${x0.toFixed(2)} ${y0.toFixed(2)} A${r} ${r} 0 ${a1 - a0 > 180 ? 1 : 0} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

function buildDials(limits) {
  const host = $("dials");
  host.textContent = "";
  for (const d of DIALS) {
    const wrap = el("div", { class: "dial" });
    const svg = el("svg", { svg: true, viewBox: "0 0 200 150", role: "img", "aria-label": d.name });
    svg.append(el("path", { svg: true, class: "g-track", d: arc(-120, 120, 80), fill: "none", stroke: "#e1e5e3", "stroke-width": 12 }));
    for (const [lo, hi, kind] of d.bands(limits)) {
      const a0 = angle(d, lo), a1 = angle(d, hi);
      if (a1 > a0) svg.append(el("path", { svg: true, d: arc(a0, a1, 80), fill: "none", stroke: BAND_COLOUR[kind], "stroke-width": 12 }));
    }
    for (let k = 0; k <= 10; k++) {  // ticks
      const a = -120 + k * 24, [x0, y0] = polar(a, 66), [x1, y1] = polar(a, k % 5 ? 71 : 74);
      svg.append(el("line", { svg: true, class: "g-tick", x1: x0, y1: y0, x2: x1, y2: y1, stroke: "#55636d", "stroke-width": k % 5 ? 1 : 2 }));
    }
    const [lx, ly] = polar(-120, 56), [hx, hy] = polar(120, 56);
    svg.append(el("text", { svg: true, class: "g-label", x: lx + 4, y: ly + 16, "font-size": 11, fill: "#55636d", "text-anchor": "middle" }, String(d.min)));
    svg.append(el("text", { svg: true, class: "g-label", x: hx - 4, y: hy + 16, "font-size": 11, fill: "#55636d", "text-anchor": "middle" }, String(d.max)));
    const needle = el("g", { svg: true, class: "needle" });
    needle.append(el("polygon", { svg: true, class: "g-ink", points: "97,100 103,100 100,28", fill: "#1f2b33" }));
    svg.append(needle, el("circle", { svg: true, class: "g-ink", cx: 100, cy: 100, r: 7, fill: "#1f2b33" }));
    const value = el("div", { class: "value" }, "–");
    const name = el("div", { class: "name" }, d.name);
    wrap.append(svg, value, name);
    host.append(wrap);
    dialEls[d.key] = { wrap, needle, value, name, d };
  }
  // Oil: a float switch (OK / low / critical), so a lamp rather than a gauge
  const oil = el("div", { class: "dial oil" });
  const lamp = el("div", { class: "oil-lamp", role: "img", "aria-label": "Oil level" });
  const word = el("div", { class: "value" }, "–");
  oil.append(lamp, word, el("div", { class: "name" }, "Oil level (switch)"));
  host.append(oil);
  oilEl = { wrap: oil, lamp, word };

  const strips = $("strips");
  strips.textContent = "";
  for (const d of STRIPS) {
    const wrap = el("div", { class: "strip" });
    const svg = el("svg", { svg: true, viewBox: "0 0 70 220", role: "img", "aria-label": d.name });
    const y = (v) => 200 - ((Math.max(d.min, Math.min(d.max, v)) - d.min) / (d.max - d.min)) * 190;
    svg.append(el("rect", { svg: true, class: "g-track-fill", x: 22, y: 10, width: 22, height: 190, rx: 4, fill: "#e1e5e3" }));
    for (const [lo, hi, kind] of d.bands(limits)) {
      if (hi > lo) svg.append(el("rect", { svg: true, x: 22, y: y(hi), width: 22, height: y(lo) - y(hi), fill: BAND_COLOUR[kind] }));
    }
    for (let k = 0; k <= 8; k++) {
      const ty = 10 + k * 190 / 8;
      svg.append(el("line", { svg: true, class: "g-tick", x1: 44, x2: k % 4 ? 49 : 53, y1: ty, y2: ty, stroke: "#55636d", "stroke-width": k % 4 ? 1 : 2 }));
    }
    svg.append(el("text", { svg: true, class: "g-label", x: 56, y: 14, "font-size": 10, fill: "#55636d" }, String(d.max)));
    svg.append(el("text", { svg: true, class: "g-label", x: 56, y: 203, "font-size": 10, fill: "#55636d" }, String(d.min)));
    const marker = el("g", { svg: true, class: "marker" });
    marker.append(el("polygon", { svg: true, class: "g-ink", points: "8,-7 8,7 20,0", fill: "#1f2b33" }),
                  el("line", { svg: true, class: "g-ink-line", x1: 18, x2: 46, y1: 0, y2: 0, stroke: "#1f2b33", "stroke-width": 3 }));
    svg.append(marker);
    const value = el("div", { class: "value" }, "–");
    wrap.append(svg, value, el("div", { class: "name" }, d.name));
    strips.append(wrap);
    stripEls[d.key] = { wrap, marker, value, d, y };
  }
}

function bandOf(d, limits, v) {
  for (const [lo, hi, kind] of d.bands(limits)) if (v >= lo && v <= hi) return kind;
  return null;
}
function showValue(node, v, d, extra) {
  node.textContent = v.toFixed(d.digits);
  node.append(el("small", {}, d.unit));
  if (extra) node.append(el("small", { class: "est" }, extra));
}
function flag(wrap, d, data, v) {
  const kind = (data.run === 2 || ALWAYS_BANDED.includes(d.key)) ? bandOf(d, data.limits, v) : null;
  wrap.classList.toggle("is-warn", kind === "warn");
  wrap.classList.toggle("is-bad", kind === "bad" || (kind === null && data.run === 2 && ["v", "f"].includes(d.key)));
}

function renderDials(data) {
  for (const { wrap, needle, value, name, d } of Object.values(dialEls)) {
    let v = data.readings[d.key];
    if (d.key === "soc_pct") {
      // Voltage only tells the charge with the engine off; while cranking or running the
      // starter or alternator skews it, so hold the last resting estimate.
      name.textContent = `Battery charge (${data.readings.vb.toFixed(2)} V)`;
      if (data.soc_reliable) lastRestSoc = v;
      wrap.title = data.soc_reliable ? "Estimated from resting voltage"
        : "Engine on: showing the last estimate from when it was off";
      if (!data.soc_reliable) {
        v = lastRestSoc;
        needle.style.transform = `rotate(${angle(d, v ?? 0)}deg)`;
        if (v === null) { value.textContent = "–"; value.append(el("small", { class: "est" }, "engine on")); }
        else showValue(value, v, d, "at rest");
        wrap.classList.remove("is-warn", "is-bad");
        continue;
      }
    }
    needle.style.transform = `rotate(${angle(d, v)}deg)`;
    showValue(value, v, d);
    flag(wrap, d, data, v);
  }
  for (const { wrap, marker, value, d, y } of Object.values(stripEls)) {
    const v = data.readings[d.key];
    marker.setAttribute("transform", `translate(0 ${y(v).toFixed(1)})`);
    showValue(value, v, d);
    flag(wrap, d, data, v);
  }
  const [kind, text] = OIL_LAMP[data.readings.oil] || ["bad", `Oil ${data.readings.oil}`];
  oilEl.lamp.className = `oil-lamp ${kind}`;
  oilEl.word.textContent = text;
  oilEl.wrap.classList.toggle("is-warn", kind === "warn");
  oilEl.wrap.classList.toggle("is-bad", kind === "bad");
}

// ---- live state ---------------------------------------------------------------------
function renderLive(data) {
  if (!Object.keys(dialEls).length) buildDials(data.limits);
  $("device-line").textContent = `${data.device_id}, boot ${data.boot}, message ${data.seq}, heartbeat every ${data.interval} s`;
  for (const li of $("lamps").children) li.classList.toggle("on", li.dataset.state === data.state);
  renderDials(data);

  const faults = $("faults");
  faults.textContent = "";
  if (data.fault_reason) faults.append(el("span", { class: "tag bad" }, data.fault_reason));
  for (const t of data.flt_text) faults.append(el("span", { class: `tag ${t.includes("(fault)") ? "bad" : "warn"}` }, t));
  if (data.breaker_tripped) faults.append(el("span", { class: "tag bad" }, "Output breaker tripped"));
  for (const w of data.warnings) faults.append(el("span", { class: "tag warn" }, w));
  if (!faults.childElementCount) faults.append(el("span", { class: "tag ok" }, "No faults or warnings"));

  const facts = $("facts");
  facts.textContent = "";
  for (const [k, v] of [["Fuel left", `${data.fuel_l} L`], ["Burn rate", `${data.fuel_l_per_h} L/h`],
                        ["Battery", `${data.readings.vb.toFixed(2)} V`],
                        ["Signal", `${data.readings.rssi} dBm`], ["Speed", `${data.speed}×`]]) {
    facts.append(el("dt", {}, k), el("dd", {}, v));
  }

  const load = $("load");
  if (document.activeElement !== load) load.value = Math.min(150, data.load_pct);
  $("load-out").textContent = `${data.load_pct} %`;
  const speed = $("speed");
  if (document.activeElement !== speed) speed.value = [...speed.options].some((o) => +o.value === data.speed) ? String(data.speed) : speed.value;
  $("btn-overheat").setAttribute("aria-pressed", String(data.overheat));
  $("btn-battery").setAttribute("aria-pressed", String(data.weak_battery));

  renderScenario(data.scenario);
  renderEvents(data.events);
}

function renderScenario(sc) {
  const p = $("sc-progress");
  p.textContent = "";
  const viewer = document.body.classList.contains("viewer");
  $("sc-stop").disabled = viewer || !(sc && sc.running);
  $("sc-start").disabled = viewer || !!(sc && sc.running);
  if (!sc) return;
  const verb = sc.running ? "Playing" : `Scenario ${sc.result}:`;
  p.append(document.createTextNode(`${verb} ${sc.name}, step ${sc.step} of ${sc.total}: ${sc.text}`));
  const bar = el("div", { class: "bar" });
  const fill = el("span");
  fill.style.width = `${Math.round(100 * sc.step / sc.total)}%`;  // CSSOM, allowed by the CSP
  bar.append(fill);
  p.append(bar);
}

function renderEvents(events) {
  const ol = $("events");
  ol.textContent = "";
  for (const e of [...events].reverse()) {
    const li = el("li", { class: /refused|error/.test(e.text) ? "refused" : "" });
    li.append(el("time", {}, wat(e.ts)), document.createTextNode(e.text));
    ol.append(li);
  }
  if (!events.length) ol.append(el("li", { class: "hint" }, "Nothing yet. Actions from this page appear here."));
}

// ---- history charts -----------------------------------------------------------------
const CHARTS = [
  { key: "v", name: "Voltage", unit: "V", min: 0, max: 260, band: (L) => [L.nominal_v * 0.95, L.nominal_v * 1.05] },
  { key: "f", name: "Frequency", unit: "Hz", min: 40, max: 56, band: (L) => [L.f_low, L.f_high] },
  { key: "load", name: "Load", unit: "%", min: 0, max: 150, band: () => [0, 100] },
  { key: "fuel", name: "Fuel", unit: "%", min: 0, max: 100, band: (L) => [L.low_fuel_warn_pct, 100] },
  { key: "t", name: "Temperature", unit: "°C", min: 0, max: 120, band: (L) => [0, L.restart_temp_c] },
  { key: "soc", name: "Battery charge", unit: "%", min: 0, max: 100, band: () => [50, 100] },
];

function renderCharts() {
  const host = $("charts");
  if (!live) return;
  host.textContent = "";
  const pts = historyPoints;
  // the window grows with the data: at least 2 min, at most 30 min
  const now = Date.now();
  const span = pts.length ? now - pts[0].ts : 0;
  const t0 = now - Math.min(30 * 60 * 1000, Math.max(2 * 60 * 1000, span));
  $("history-title").textContent = `Last ${Math.round((now - t0) / 60000)} minutes`;
  const x = (ts) => ((ts - t0) / (now - t0)) * 300;
  for (const c of CHARTS) {
    const box = el("div", { class: "chart" });
    const last = pts.length ? pts[pts.length - 1][c.key] : null;
    const h3 = el("h3", {}, c.name);
    h3.append(el("span", {}, last === null ? "–" : `${last} ${c.unit}`));
    const svg = el("svg", { svg: true, viewBox: "0 0 300 70", preserveAspectRatio: "none", role: "img",
                            "aria-label": `${c.name}, last 30 minutes` });
    const y = (v) => 66 - ((Math.max(c.min, Math.min(c.max, v)) - c.min) / (c.max - c.min)) * 62;
    const [b0, b1] = c.band(live.limits);
    svg.append(el("rect", { svg: true, class: "band", x: 0, width: 300, y: y(b1), height: Math.max(0, y(b0) - y(b1)) }));
    let runStart = null;  // shade running periods, mark faults
    pts.forEach((p, idx) => {
      if (p.run === 2 && runStart === null) runStart = p.ts;
      const endRun = runStart !== null && (p.run !== 2 || idx === pts.length - 1);
      if (endRun) {
        svg.append(el("rect", { svg: true, class: "run", x: x(runStart), y: 0, width: Math.max(1, x(p.ts) - x(runStart)), height: 70 }));
        runStart = null;
      }
      if (p.run === 4 && (idx === 0 || pts[idx - 1].run !== 4)) {
        svg.append(el("line", { svg: true, class: "fault", x1: x(p.ts), x2: x(p.ts), y1: 0, y2: 70 }));
      }
    });
    if (pts.length > 1) {
      svg.append(el("polyline", { svg: true, class: "line",
        points: pts.map((p) => `${x(p.ts).toFixed(1)},${y(p[c.key]).toFixed(1)}`).join(" ") }));
    }
    box.append(h3, svg);
    host.append(box);
  }
}

// ---- cloud view ---------------------------------------------------------------------
function ago(ms) {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  return s < 90 ? `${s} s ago` : `${Math.round(s / 60)} min ago`;
}
const RUN_NAMES = ["Off", "Cranking", "Running", "Stopping", "Fault"];
function ruleName(a) {
  const rule = (a && a.rule) || "";
  if (!rule) return "Alert";
  if (rule.includes("missing-telemetry")) {
    // The window comes from the alert's own description, so it follows the Terraform setting.
    const m = /last (\d+) minutes?/.exec((a && a.description) || "");
    return m ? `No telemetry for ${m[1]} min` : "No telemetry received";
  }
  if (rule.includes("latency")) return "Cloud processing slow";
  if (rule === "device-offline") return "Device offline";
  if (rule.includes("iothub-quota")) {
    const q = /quota-(\d+)/.exec(rule);
    return q ? `IoT Hub daily quota ${q[1]} % used` : "IoT Hub daily quota nearly used";
  }
  return rule;
}
function renderCloud(c) {
  const host = $("cloud");
  host.textContent = "";
  if (!c.configured) {
    host.append(el("p", { class: "hint" }, "Cloud view is off. Set panel.api_url in config/default.yaml or EMS_API_URL to the staging API."));
    return;
  }
  if (!c.key_set) host.append(el("p", { class: "hint warn" }, "No API key: add the EMS_API_KEY Codespace secret, then restart the simulator."));
  const resp = c.state || {};
  const tw = c.twin || {};
  const dl = el("dl");
  const row = (k, v, cls) => dl.append(el("dt", {}, k), el("dd", cls ? { class: cls } : {}, v));
  if (resp.error) row("Stored state", resp.error);
  else if (resp.status) {                      // API since PR #14
    const r = resp.readings || {};
    const received = resp.received_at ? Date.parse(resp.received_at) : null;
    row("Last message", received ? `${ago(received)} (${wat(received)})` : "–");
    row("Stored state", resp.status.run_state || "–");
    row("Stored readings", r.v !== undefined
      ? `${r.v} V, ${r.f} Hz, load ${r.load_pct} %, fuel ${r.fuel} %, battery ${r.soc_pct} %${r.soc_reliable ? "" : " (est.)"}` : "–");
    // Online/Offline from IoT Hub connect/disconnect events (Event Grid); red when Offline
    const link = resp.link || {};
    if (link.connection_status) {
      const since = link.last_status_change ? Date.parse(link.last_status_change) : null;
      const when = since ? ` since ${wat(since)} (${ago(since)})` : "";
      row("Connection", `${link.connection_status}${when}`, link.connection_status === "Offline" ? "conn-off" : "conn-on");
    } else if ("connection_status" in link) {
      row("Connection", "no connect/disconnect event yet");
    }
  } else {                                     // older API shape
    const st = resp.state || {};
    row("Last message", st.ts ? `${ago(st.ts)} (${wat(st.ts)})` : "–");
    row("Stored state", st.run !== undefined ? RUN_NAMES[st.run] || st.run : "–");
    row("Stored readings", st.v !== undefined ? `${st.v} V, ${st.f} Hz, fuel ${st.fuel} %` : "–");
  }
  if (tw.error) row("Twin", tw.error);
  else {
    row("IoT Hub sees", tw.connection_state || "–");
    const desired = Object.entries(tw.desired || {}).filter(([k]) => !k.startsWith("$"));
    row("Remote settings", desired.length ? desired.map(([k, v]) => `${k} ${v}`).join(", ") : "none (config values)");
    const statuses = Object.entries((tw.reported && tw.reported.settings_status) || {});
    const rejected = statuses.filter(([, s]) => s && s.status === "rejected");
    if (rejected.length) row("Rejected", rejected.map(([k, s]) => `${k}: ${s.reason}`).join("; "));
  }
  host.append(dl);

  host.append(el("h3", {}, "Alerts"));
  const al = c.alerts || {};
  const sys = c.system_alerts || {};
  if (al.error) host.append(el("p", { class: "hint" }, `Couldn't read alerts: ${al.error}`));
  else {
    const items = [];
    const add = (list, prefix, nResolved) => {
      const active = list.filter((a) => a.status === "active");
      const resolved = list.filter((a) => a.status !== "active").slice(0, nResolved);
      for (const a of active) {
        items.push(el("li", { class: "tag bad" }, `${prefix}${ruleName(a)} since ${a.fired_at ? watDay(Date.parse(a.fired_at)) : "–"}`));
      }
      for (const a of resolved) {
        items.push(el("li", { class: "tag ok" }, `${prefix}${ruleName(a)}: resolved ${a.resolved_at ? watDay(Date.parse(a.resolved_at)) : ""}`));
      }
    };
    add(al.alerts || [], "", 3);
    if (!sys.error) add(sys.alerts || [], "System: ", 1);   // no device: affects the whole pipeline
    if (!items.length) host.append(el("p", { class: "allgood" }, "No alerts."));
    else {
      const ul = el("ul", { class: "alerts" });
      ul.append(...items);
      host.append(ul);
    }
  }

  host.append(el("h3", {}, "Checks"));
  if (c.checks && c.checks.length) {
    const ul = el("ul", { class: "checks" });
    for (const n of c.checks) ul.append(el("li", {}, n));
    host.append(ul);
  } else host.append(el("p", { class: "allgood" }, "Device and cloud agree."));
  host.append(el("p", { class: "hint" }, `Checked ${ago(c.fetched_at)} from ${c.api_url}. Times in WAT. Twin read every 2 min to save IoT Hub messages.`));
}

// ---- polling ------------------------------------------------------------------------
async function pollLive() {
  try {
    live = await api("/api/live");
    renderLive(live);
    if (offline) { offline = false; $("offline").hidden = true; }
  } catch {
    offline = true;
    $("offline").hidden = false;
  }
}
async function pollHistory() {
  try {
    const data = await api(`/api/history?since=${lastHistoryTs}`);
    historyPoints = historyPoints.concat(data.points).filter((p) => p.ts > Date.now() - 30 * 60 * 1000);
    if (historyPoints.length) lastHistoryTs = historyPoints[historyPoints.length - 1].ts;
    renderCharts();
  } catch { /* shown by pollLive */ }
}
async function pollCloud() {
  try { renderCloud(await api("/api/cloud")); } catch (e) { renderCloud({ configured: true, state: { error: e.message }, twin: {} }); }
}
function every(ms, fn) {
  fn();
  setInterval(() => { if (!document.hidden) fn(); }, ms);
}

// ---- actions ------------------------------------------------------------------------
async function act(action, value) {
  try { await api("/api/action", value === undefined ? { action } : { action, value }); }
  catch (e) { live && live.events.push({ ts: Date.now(), text: `${action}: error ${e.message}` }); }
  pollLive();
}

// ---- theme ------------------------------------------------------------------------------
// Light or dark; remembered in this browser, otherwise follows the system setting.
const THEME_KEY = "ems-panel-theme";
function savedTheme() {
  try { return localStorage.getItem(THEME_KEY); } catch { return null; }
}
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const btn = $("theme");
  btn.setAttribute("aria-pressed", String(theme === "dark"));
  btn.textContent = theme === "dark" ? "Light theme" : "Dark theme";
}
function initTheme() {
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(savedTheme() || (prefersDark ? "dark" : "light"));
  $("theme").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch { /* private mode: not remembered */ }
  });
}

// Shared panel: show who you are signed in as; viewers get the controls disabled.
async function initSession() {
  let s = { sharing: false, role: "operator" };
  try { s = await api("/api/session"); } catch { return; }
  if (!s.sharing) return;
  const tools = document.querySelector(".plate-tools");
  tools.append(el("span", { class: `role-badge ${s.role}` }, s.role === "operator" ? "Operator" : "Viewer"));
  const out = el("button", { class: "btn", type: "button", id: "sign-out" }, "Sign out");
  out.addEventListener("click", async () => {
    await api("/api/logout", {}).catch(() => {});
    window.location.replace("/login");
  });
  tools.append(out);
  if (s.role !== "operator") {
    document.body.classList.add("viewer");
    const controls = document.querySelector(".controls");
    controls.querySelectorAll("button, input, select").forEach((n) => { n.disabled = true; });
    controls.prepend(el("p", { class: "hint view-only-note" },
      "View only: you signed in with the viewer PIN. The operator PIN unlocks the controls."));
  }
}

async function init() {
  initTheme();
  await initSession();
  document.querySelectorAll("[data-action]").forEach((b) => b.addEventListener("click", () => act(b.dataset.action)));
  $("load").addEventListener("input", (e) => { $("load-out").textContent = `${e.target.value} %`; });
  $("load").addEventListener("change", (e) => act("load", Number(e.target.value)));
  $("speed").addEventListener("change", (e) => act("speed", Number(e.target.value)));

  const scSel = $("scenario");
  try {
    const { scenarios } = await api("/api/scenarios");
    for (const s of scenarios) scSel.append(el("option", { value: s.name }, `${s.name} (${s.speed}×)`));
    const describe = () => {
      const s = scenarios.find((x) => x.name === scSel.value);
      $("sc-desc").textContent = s ? `${s.description}. ${s.steps} steps.` : "";
    };
    scSel.addEventListener("change", describe);
    describe();
  } catch { /* offline banner covers it */ }
  $("sc-start").addEventListener("click", async () => {
    try { await api("/api/scenario/start", { name: scSel.value }); } catch (e) { alertInline(e.message); }
    pollLive();
  });
  $("sc-stop").addEventListener("click", async () => { await api("/api/scenario/stop", {}).catch(() => {}); pollLive(); });

  every(1000, pollLive);
  every(5000, pollHistory);
  every(10000, pollCloud);
  setInterval(() => { if (!document.hidden) renderCharts(); }, 15000);  // keep the time axis moving
}

function alertInline(msg) {
  $("sc-progress").textContent = msg;
}

init();
