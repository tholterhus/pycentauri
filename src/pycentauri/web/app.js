// pycentauri web console
// Consumes /api/info, /status, /events/status (SSE), /attributes.
// Posts to /print/{pause,resume,stop} and /print/{speed,fan,temperature}
// when --enable-control is on.

const $ = (id) => document.getElementById(id);

// PrintInfo.Status → display label + semantic class.
// Table is authoritative, lifted from Elegoo's own elegoo-link SDK
// (src/lan/adapters/elegoo_fdm_cc/elegoo_fdm_cc_message_adapter.cpp:33–62).
// Codes 2/3/4 and 23–26 are resin-printer / LCD-specific — kept here for
// completeness in case a future firmware surfaces them on the Carbon.
const PRINT_STATUS = {
  0:  { label: "IDLE",         cls: "state-idle" },
  1:  { label: "HOMING",       cls: "state-printing" },
  2:  { label: "DROPPING",     cls: "state-printing" },
  3:  { label: "EXPOSING",     cls: "state-printing" },
  4:  { label: "LIFTING",      cls: "state-printing" },
  5:  { label: "PAUSING",      cls: "state-paused" },
  6:  { label: "PAUSED",       cls: "state-paused" },
  7:  { label: "STOPPING",     cls: "state-stopped" },
  8:  { label: "STOPPED",      cls: "state-stopped" },
  9:  { label: "COMPLETED",    cls: "state-completed" },
  10: { label: "FILE CHECK",   cls: "state-printing" },
  11: { label: "SELF CHECK",   cls: "state-printing" },
  12: { label: "RESUMING",     cls: "state-printing" },
  13: { label: "PRINTING",     cls: "state-printing" },
  14: { label: "ERROR",        cls: "state-err" },
  15: { label: "LEVELING",     cls: "state-printing" },
  16: { label: "PREHEATING",   cls: "state-printing" },
  17: { label: "RESONANCE",    cls: "state-printing" },
  18: { label: "PRINT START",  cls: "state-printing" },
  19: { label: "LEVEL DONE",   cls: "state-printing" },
  20: { label: "PREHEAT DONE", cls: "state-printing" },
  21: { label: "HOMING DONE",  cls: "state-printing" },
  22: { label: "RESONANCE OK", cls: "state-printing" },
  23: { label: "AUTO FEED",    cls: "state-printing" },
  24: { label: "UNLOADING",    cls: "state-printing" },
  25: { label: "UNLOAD ERR",   cls: "state-err" },
  26: { label: "UNLOAD PAUSE", cls: "state-paused" },
  // CC2-only codes (mapped from machine_status + sub_status in cc2.py)
  27: { label: "SWITCHING FILAMENT", cls: "state-printing" },
  28: { label: "FILAMENT LOADED",    cls: "state-printing" },
  29: { label: "UNLOADING FILAMENT", cls: "state-printing" },
};

function fmtSeconds(s) {
  if (s == null || Number.isNaN(+s)) return "—";
  s = Math.max(0, Math.round(+s));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${m.toString().padStart(2, "0")}m`;
  if (m > 0) return `${m}m ${sec.toString().padStart(2, "0")}s`;
  return `${sec}s`;
}

function fmtTemp(v, padTo = "---.-") {
  if (v == null || Number.isNaN(+v)) return padTo;
  return (+v).toFixed(1);
}

function fmtCoord(v) {
  if (v == null || Number.isNaN(+v)) return "---.--";
  return (+v).toFixed(2).padStart(6, " ");
}

function tempPct(actual, target) {
  if (actual == null || target == null || target <= 0) return 0;
  return Math.min(100, Math.max(0, (actual / target) * 100));
}

function setStatusPill(kind, label) {
  const pill = $("status-pill");
  pill.classList.remove("warn", "err");
  if (kind === "warn") pill.classList.add("warn");
  if (kind === "err")  pill.classList.add("err");
  $("status-text").textContent = label;
}

// ---------------------------------------------------------------------------

async function loadInfo() {
  try {
    const r = await fetch("/api/info");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const info = await r.json();

    $("version").textContent = info.version || "—";
    const badge = $("update-badge");
    if (info.update_available && info.latest_version) {
      badge.textContent = `↑ v${info.latest_version}`;
      badge.href = `https://github.com/bjan/pycentauri/releases/tag/v${info.latest_version}`;
      badge.title = `pycentauri ${info.latest_version} is available (you have ${info.version})`;
      badge.hidden = false;
    } else {
      badge.hidden = true;
    }
    $("host").textContent = info.printer_host || "—";
    $("mainboard").textContent = info.mainboard_id || "—";
    $("mainboard").title = info.mainboard_id || "";

    if (info.enable_control) {
      $("controls").hidden = false;
      $("adjust").hidden = false;
      canvasControlEnabled = true;
      filesControlEnabled = true;
      heatingControlEnabled = true;
    }

    try {
      const attrs = await (await fetch("/attributes")).json();
      $("printer-name").textContent = (attrs.machine_name || attrs.name || "Centauri Carbon").toUpperCase();
      $("firmware").textContent = attrs.firmware_version ? `FW ${attrs.firmware_version}` : "";
    } catch (_) { /* firmware may not push in idle */ }

    if (info.connected) {
      setStatusPill("ok", "LINK OK");
    } else {
      setStatusPill("warn", "LINK DOWN");
    }
  } catch (e) {
    setStatusPill("err", "SRV OFFLINE");
    console.warn("loadInfo failed:", e);
  }
}

function renderStatus(raw) {
  const pi = raw.PrintInfo || {};
  const pstatus = pi.Status;
  const filename = pi.Filename;

  // Job name
  $("filename").textContent = filename || "— · AWAITING ·";

  // Progress
  const progress = pi.Progress;
  const pct = (progress != null) ? Math.max(0, Math.min(100, +progress)) : 0;
  $("progress-bar").style.width = pct + "%";
  $("progress-label").textContent = (progress != null) ? `${pct}%` : "—";

  // State word + code
  const info = PRINT_STATUS[pstatus];
  const sw = $("print-status");
  const sc = $("print-status-code");
  sw.classList.remove(
    "state-idle", "state-printing", "state-paused",
    "state-stopped", "state-completed", "state-err",
  );
  if (info) {
    sw.textContent = info.label;
    sw.classList.add(info.cls);
  } else if (pstatus != null) {
    sw.textContent = `CODE·${pstatus}`;
  } else {
    sw.textContent = "—";
  }
  sc.textContent = (pstatus != null) ? String(pstatus).padStart(2, "0") : "--";

  // Controls: nur die passenden Buttons zeigen (Tim 7.11.) — läuft: Pause,
  // pausiert (manuell/Spaghetti/Filament): Resume. Stop bleibt immer da.
  const paused = info?.cls === "state-paused";
  const grid = document.querySelector("#controls .btn-grid");
  if (grid && $("btn-pause") && $("btn-resume")) {
    $("btn-pause").hidden = paused;
    $("btn-resume").hidden = !paused;
    grid.classList.add("two-btns");
  }

  // Layer — show current even when the total is unknown (the CC2's status
  // has no total-layer field; it's filled from file metadata when available).
  $("layer").textContent =
    pi.CurrentLayer == null ? "—"
    : pi.TotalLayer ? `${pi.CurrentLayer} / ${pi.TotalLayer}`
    : `${pi.CurrentLayer}`;

  // Times (CurrentTicks / TotalTicks are seconds on CC firmware)
  const elapsed = pi.CurrentTicks;
  const total = pi.TotalTicks;
  $("elapsed").textContent = fmtSeconds(elapsed);
  $("remaining").textContent = (total != null && elapsed != null)
    ? fmtSeconds(total - elapsed) : "—";

  // Speed %
  $("speed").textContent = (pi.PrintSpeedPct != null) ? `${pi.PrintSpeedPct}%` : "—";
  reflectSpeedMode(pi.PrintSpeedPct);
  reflectLight(raw);

  // Hydrate ADJUST controls from live values + retune poll cadence.
  hydrateAdjust(raw);
  noteStatusForPoll(pstatus);

  // Temperatures
  const noz  = raw.TempOfNozzle, nozT = raw.TempTargetNozzle;
  const bed  = raw.TempOfHotbed, bedT = raw.TempTargetHotbed;
  const cham = raw.TempOfBox,    chamT = raw.TempTargetBox;
  $("t-nozzle").textContent        = fmtTemp(noz);
  $("t-nozzle-target").textContent = (nozT != null) ? String(Math.round(+nozT)) : "---";
  $("t-nozzle-bar").style.width    = tempPct(noz, nozT) + "%";
  $("t-bed").textContent           = fmtTemp(bed);
  $("t-bed-target").textContent    = (bedT != null) ? String(Math.round(+bedT)) : "---";
  $("t-bed-bar").style.width       = tempPct(bed, bedT) + "%";
  $("t-chamber").textContent       = fmtTemp(cham);
  $("t-chamber-target").textContent = (chamT != null) ? String(Math.round(+chamT)) : "---";
  // Chamber often targets 0; show the actual vs a reference of 50°C so the bar conveys warmth.
  $("t-chamber-bar").style.width   = Math.min(100, Math.max(0, (+(cham ?? 0) / 50) * 100)) + "%";

  // Coords "x,y,z"
  if (typeof raw.CurrenCoord === "string") {
    const [x, y, z] = raw.CurrenCoord.split(",");
    $("pos-x").textContent = fmtCoord(x);
    $("pos-y").textContent = fmtCoord(y);
    $("pos-z").textContent = fmtCoord(z);
  }

  // Z offset
  if (raw.ZOffset != null) $("z-offset").textContent = (+raw.ZOffset).toFixed(3);

  // Fans
  const fans = raw.CurrentFanSpeed || {};
  const setFan = (id, key, rowId) => {
    const v = fans[key];
    $(id).textContent = (v != null) ? `${v}%` : "—";
    if (rowId && v != null) $(rowId).hidden = false;
  };
  setFan("fan-model", "ModelFan");
  setFan("fan-aux",   "AuxiliaryFan");
  setFan("fan-box",   "BoxFan");
  setFan("fan-controller", "ControllerFan", "fan-controller-row");
  setFan("fan-heater",     "HeaterFan",     "fan-heater-row");

  // CC2-only: live head speed from gcode_move.speed (mm/min)
  const cc2 = raw._cc2;
  if (cc2 && cc2.gcode_move_speed != null) {
    // gcode_move.speed is the commanded speed of the current move in
    // mm/min; ÷60 gives the mm/s figure the printer's own screen shows
    // (verified against the screen 2026-07-05). Travels spike it briefly.
    $("head-speed-row").hidden = false;
    $("head-speed").textContent = `${Math.round(cc2.gcode_move_speed / 60)} mm/s`;
  }

  setStatusPill("ok", "LINK OK");
}

async function pollOnce() {
  try {
    const r = await fetch("/status");
    if (!r.ok) throw new Error("HTTP " + r.status);
    const body = await r.json();
    if (body.raw) renderStatus(body.raw);
  } catch (e) {
    setStatusPill("warn", "LINK WAIT");
    console.warn("poll failed:", e);
  }
}

function connectSSE() {
  let src;
  try { src = new EventSource("/events/status"); }
  catch (_) {
    setStatusPill("warn", "SSE N/A");
    return;
  }
  src.addEventListener("status", (ev) => {
    try { renderStatus(JSON.parse(ev.data)); } catch (_) {}
  });
  // SSE in Firefox is prone to silent stalls — the connection appears
  // open but no events arrive. `onerror` fires once and the browser's
  // auto-reconnect doesn't always kick in. The pollOnce safety net in
  // main() compensates regardless; here we just surface the wait state.
  src.onerror = () => {
    setStatusPill("warn", "LINK WAIT");
  };
}

// ---------------------------------------------------------------------------
// Controls

function setMsg(text, kind) {
  const el = $("ctrl-msg");
  el.classList.remove("ok", "warn", "err");
  if (kind) el.classList.add(kind);
  el.textContent = text;
}

async function doAction(label, path, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  setMsg(`» ${label}…`);
  const btns = document.querySelectorAll(".controls button");
  for (const b of btns) b.disabled = true;
  try {
    const r = await fetch(path, { method: "POST" });
    if (r.status === 504) {
      // Command reached the printer but confirmation didn't arrive in
      // time (CC2 lifecycle commands respond only after the mechanical
      // sequence finishes). Almost always the action still completes.
      setMsg(`⧗ ${label} sent — no confirmation yet; watch the status`, "warn");
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`);
    setMsg(`✓ ${label} acknowledged`, "ok");
  } catch (e) {
    setMsg(`✗ ${label}: ${e.message}`, "err");
  } finally {
    setTimeout(() => {
      for (const b of btns) b.disabled = false;
    }, 800);
  }
}

async function doUpload() {
  const input = $("upload-file");
  const file = input?.files?.[0];
  if (!file) { setMsg("Choose a file to upload first.", "warn"); return; }
  const start = $("upload-start")?.checked;
  const fd = new FormData();
  fd.append("file", file, file.name);
  fd.append("start", start ? "true" : "false");

  const btns = document.querySelectorAll(".controls button");
  for (const b of btns) b.disabled = true;
  // The browser->server leg is fast on a LAN; the slow, invisible part is
  // the server chunking to the printer. Show an honest indeterminate state.
  setMsg(`» uploading ${file.name}…`);
  try {
    const r = await fetch("/files/upload", { method: "POST", body: fd });
    if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`);
    const data = await r.json();
    setMsg(`✓ uploaded ${data.filename}${start ? " — print started" : ""}`, "ok");
    input.value = "";
    const un = $("upload-name"); if (un) un.textContent = "";
    setTimeout(loadFiles, 1500); // reflect the new file in the Files panel

  } catch (e) {
    setMsg(`✗ upload: ${e.message}`, "err");
  } finally {
    for (const b of btns) b.disabled = false;
  }
}

function wireControls() {
  $("btn-pause")?.addEventListener("click", () =>
    doAction("PAUSE", "/print/pause", "Pause the current print?")
  );
  $("btn-resume")?.addEventListener("click", () =>
    doAction("RESUME", "/print/resume")
  );
  $("btn-stop")?.addEventListener("click", () =>
    doAction("STOP", "/print/stop",
      "Stop the current print?\n\nThis cannot be undone.")
  );
  $("btn-upload")?.addEventListener("click", doUpload);
  $("upload-file")?.addEventListener("change", (ev) => {
    const un = $("upload-name");
    if (un) un.textContent = ev.target?.files?.[0]?.name || "";
  });

  // Detection threshold slider (gear next to the Threshold value).
  $("btn-threshold")?.addEventListener("click", () => {
    const cur = parseFloat($("detect-threshold").textContent) || 0.5;
    const sl = $("threshold-slider");
    sl.value = String(cur);
    $("threshold-value").textContent = cur.toFixed(2);
    $("threshold-modal").hidden = false;
  });
  $("threshold-cancel")?.addEventListener("click", () => {
    $("threshold-modal").hidden = true;
  });
  $("threshold-slider")?.addEventListener("input", (ev) => {
    $("threshold-value").textContent = parseFloat(ev.target.value).toFixed(2);
  });
  $("threshold-set")?.addEventListener("click", async () => {
    const v = parseFloat($("threshold-slider").value);
    const btn = $("threshold-set");
    btn.disabled = true;
    try {
      const r = await fetch("/api/detect/threshold", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ threshold: v }),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`);
      $("threshold-modal").hidden = true;
    } catch (e) {
      $("threshold-value").textContent = "✗ " + e.message;
    } finally {
      btn.disabled = false;
    }
  });

  // Keyboard shortcuts when controls are visible.
  document.addEventListener("keydown", (ev) => {
    if ($("controls").hidden) return;
    if (ev.target && /^(INPUT|TEXTAREA|SELECT)$/.test(ev.target.tagName)) return;
    if (ev.key === "F1" && !$("btn-pause").hidden) { ev.preventDefault(); $("btn-pause").click(); }
    if (ev.key === "F2" && !$("btn-resume").hidden) { ev.preventDefault(); $("btn-resume").click(); }
    if (ev.key === "F3") { ev.preventDefault(); $("btn-stop").click(); }
  });
}

// ---------------------------------------------------------------------------
// Live adjust (Cmd 403 family) — print speed, fan, temperature

function setAdjMsg(text, kind) {
  const el = $("adj-msg");
  el.classList.remove("ok", "warn", "err");
  if (kind) el.classList.add(kind);
  el.textContent = text || "";
}

// Two-way bind a slider/number pair sharing a base id (`${base}` for number,
// `${base}-slider` for slider). Returns a getter for the current value.
// Every user edit marks the row dirty: the background hydration then leaves
// it alone until the value is applied (or the page reloads) — otherwise the
// ~1/s status push would yank the control back mid-edit. The row's Set
// button glows amber while dirty so "press Set" is obvious.
const adjustDirty = new Set();
let liveNozzleTarget = null;

function markAdjustDirty(base) {
  adjustDirty.add(base);
  const btn = document.querySelector(`.adj-apply[data-target="${base}"]`);
  if (btn) btn.classList.add("dirty");
}

function clearAdjustDirty(base) {
  adjustDirty.delete(base);
  const btn = document.querySelector(`.adj-apply[data-target="${base}"]`);
  if (btn) btn.classList.remove("dirty");
}

function bindPair(base) {
  const num = $(`adj-${base}`);
  const slider = $(`adj-${base}-slider`);
  if (!num || !slider) return () => 0;
  const clamp = (v) =>
    Math.max(+num.min, Math.min(+num.max, Number.isFinite(+v) ? +v : 0));
  slider.addEventListener("input", () => {
    markAdjustDirty(base);
    num.value = String(clamp(slider.value));
  });
  num.addEventListener("input", () => {
    markAdjustDirty(base);
    slider.value = String(clamp(num.value));
  });
  return () => clamp(num.value);
}

const ADJUST_TARGETS = {
  "fan-model": {
    label: "MODEL FAN",
    path: "/print/fan",
    build: (g) => ({ model: g("fan-model") }),
  },
  "fan-aux": {
    label: "AUX FAN",
    path: "/print/fan",
    build: (g) => ({ auxiliary: g("fan-aux") }),
  },
  "fan-chamber": {
    label: "CHAMBER FAN",
    path: "/print/fan",
    build: (g) => ({ chamber: g("fan-chamber") }),
  },
  "temp-nozzle": {
    label: "NOZZLE TEMP",
    path: "/print/temperature",
    build: (g) => ({ nozzle: g("temp-nozzle") }),
    confirm: (g) => {
      const v = g("temp-nozzle");
      if (v > 240) return `Set nozzle target to ${v}°C ? High-temp; check filament rating.`;
      // A big jump against the current target mid-print is almost always a
      // slip (260° printing, slider lands on 120°) — ask before wrecking
      // the material.
      const cur = liveNozzleTarget;
      if (cur > 0 && Math.abs(v - cur) / cur > 0.10) {
        return `Set nozzle target to ${v}°C ? Current target is ${Math.round(cur)}°C — that is more than 10% off.`;
      }
      return null;
    },
  },
  "temp-bed": {
    label: "BED TEMP",
    path: "/print/temperature",
    build: (g) => ({ bed: g("temp-bed") }),
    confirm: (g) => g("temp-bed") > 85
      ? `Set bed target to ${g("temp-bed")}°C ? High-temp; check bed adhesive.`
      : null,
  },
  "temp-chamber": {
    label: "CHAMBER TEMP",
    path: "/print/temperature",
    build: (g) => ({ chamber: g("temp-chamber") }),
  },
};

async function applyAdjust(target, getters) {
  const spec = ADJUST_TARGETS[target];
  if (!spec) return;
  const get = (k) => getters[k] ? getters[k]() : 0;

  const confirmMsg = spec.confirm ? spec.confirm(get) : null;
  if (confirmMsg && !confirm(confirmMsg)) {
    // Declined: the edit is abandoned — the control returns to the live
    // value on the next push instead of staying dirty forever.
    clearAdjustDirty(target);
    return;
  }

  setAdjMsg(`» ${spec.label}…`);
  const btns = document.querySelectorAll("#adjust .btn");
  for (const b of btns) b.disabled = true;

  try {
    const r = await fetch(spec.path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(spec.build(get)),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`);
    clearAdjustDirty(target);  // confirmed — the live value may re-hydrate
    setAdjMsg(`✓ ${spec.label} acknowledged`, "ok");
  } catch (e) {
    setAdjMsg(`✗ ${spec.label}: ${e.message}`, "err");
  } finally {
    setTimeout(() => { for (const b of btns) b.disabled = false; }, 600);
  }
}

// Hydrate the ADJUST sliders/inputs from a status push. Skips controls that
// have focus OR carry an unapplied user edit (dirty) — the ~1/s push must
// never yank a value out from under the user mid-drag or mid-type.
function hydrateAdjust(raw) {
  if (!$("adjust")) return;
  liveNozzleTarget = raw.TempTargetNozzle ?? null;
  const fans = raw.CurrentFanSpeed || {};
  const targets = [
    ["fan-model",    fans.ModelFan],
    ["fan-aux",      fans.AuxiliaryFan],
    ["fan-chamber",  fans.BoxFan],
    ["temp-nozzle",  raw.TempTargetNozzle],
    ["temp-bed",     raw.TempTargetHotbed],
    ["temp-chamber", raw.TempTargetBox],
  ];
  for (const [base, v] of targets) {
    if (v == null) continue;
    if (adjustDirty.has(base)) continue;  // unapplied user edit — hands off
    const num = $(`adj-${base}`);
    const slider = $(`adj-${base}-slider`);
    if (!num || !slider) continue;
    if (document.activeElement === num || document.activeElement === slider) continue;
    const rounded = String(Math.round(+v));
    num.value = rounded;
    slider.value = rounded;
  }
}

async function applySpeedMode(mode) {
  setAdjMsg(`» SPEED MODE → ${mode.toUpperCase()}…`);
  const btns = document.querySelectorAll("#adjust .btn");
  for (const b of btns) b.disabled = true;
  try {
    const r = await fetch("/print/speed", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`);
    setAdjMsg(`✓ SPEED MODE → ${mode.toUpperCase()} acknowledged · only effective mid-print`, "ok");
  } catch (e) {
    setAdjMsg(`✗ SPEED MODE: ${e.message}`, "err");
  } finally {
    setTimeout(() => { for (const b of btns) b.disabled = false; }, 600);
  }
}

// Visually highlight the mode that matches the live PrintSpeedPct.
function reflectSpeedMode(pct) {
  if (pct == null) return;
  for (const btn of document.querySelectorAll("#adj-speed-modes .adj-mode")) {
    const v = { silent: 50, balanced: 100, sport: 130, ludicrous: 160 }[btn.dataset.mode];
    btn.classList.toggle("active", +pct === v);
  }
}

function wireAdjust() {
  if (!$("adjust")) return;
  const getters = {
    "fan-model":     bindPair("fan-model"),
    "fan-aux":       bindPair("fan-aux"),
    "fan-chamber":   bindPair("fan-chamber"),
    "temp-nozzle":   bindPair("temp-nozzle"),
    "temp-bed":      bindPair("temp-bed"),
    "temp-chamber":  bindPair("temp-chamber"),
  };
  for (const btn of document.querySelectorAll("#adjust .adj-apply")) {
    btn.addEventListener("click", () => applyAdjust(btn.dataset.target, getters));
  }
  for (const btn of document.querySelectorAll("#adj-speed-modes .adj-mode")) {
    btn.addEventListener("click", () => applySpeedMode(btn.dataset.mode));
  }
  for (const btn of document.querySelectorAll("#adj-light .adj-light")) {
    btn.addEventListener("click", () => applyLight(btn.dataset.light === "on"));
  }
}

async function applyLight(on) {
  const btns = document.querySelectorAll("#adj-light .adj-light");
  for (const b of btns) b.disabled = true;
  try {
    const r = await fetch("/light", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status} — ${await r.text()}`);
    setAdjMsg(`✓ LIGHT → ${on ? "ON" : "OFF"} acknowledged`, "ok");
  } catch (e) {
    setAdjMsg(`✗ LIGHT: ${e.message}`, "err");
  } finally {
    setTimeout(() => { for (const b of btns) b.disabled = false; }, 600);
  }
}

// Highlight the active light state from the live status (LightStatus.SecondLight).
function reflectLight(raw) {
  const on = raw && raw.LightStatus && raw.LightStatus.SecondLight;
  const btnOn = document.querySelector('#adj-light [data-light="on"]');
  const btnOff = document.querySelector('#adj-light [data-light="off"]');
  if (btnOn) btnOn.classList.toggle("active", !!on);
  if (btnOff) btnOff.classList.toggle("active", !on);
}

// ---------------------------------------------------------------------------
// RTSP bridge

function setRtspMsg(text, kind) {
  const el = $("rtsp-msg");
  el.classList.remove("ok", "err", "warn");
  if (kind) el.classList.add(kind);
  el.textContent = text || "";
}

function renderRtsp(info) {
  const panel = $("rtsp-panel");
  if (!info || !info.enabled) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;

  const state = $("rtsp-state");
  const btn   = $("btn-rtsp-toggle");
  const lbl   = $("btn-rtsp-label");
  const url   = $("rtsp-url");
  const meta  = $("rtsp-meta");

  // URL — prefer the advertised URL that uses the hostname the user hit us with.
  const shown = (info.advertised_urls && info.advertised_urls[0]) ||
                (info.urls && info.urls[0]) ||
                "rtsp://—";
  url.textContent = shown;
  url.classList.toggle("dim", !info.running);

  // Meta: port, path, fps, bitrate
  const parts = [];
  if (info.port)    parts.push(`PORT ${info.port}`);
  if (info.path)    parts.push(`PATH /${info.path}`);
  if (info.fps)     parts.push(`FPS ${info.fps}`);
  if (info.bitrate) parts.push(`BR ${info.bitrate}`);
  meta.textContent = parts.join("   ·   ");

  // State + button
  state.classList.remove("on", "off", "err", "na");
  if (!info.available) {
    state.textContent = "N/A";
    state.classList.add("na");
    lbl.textContent = "N/A";
    btn.disabled = true;
    setRtspMsg(info.reason || "MediaMTX or ffmpeg not available.", "err");
    return;
  }
  if (info.running) {
    state.textContent = "ONLINE";
    state.classList.add("on");
    lbl.textContent = "STOP";
  } else {
    state.textContent = "OFFLINE";
    state.classList.add("off");
    lbl.textContent = "START";
  }
  btn.disabled = false;

  if (info.reason && !info.running) {
    setRtspMsg(info.reason, "warn");
  } else if (info.running) {
    setRtspMsg("MediaMTX running · ffmpeg transcode is on-demand", "ok");
  } else {
    setRtspMsg("", null);
  }
}

async function loadRtsp() {
  try {
    const r = await fetch("/api/rtsp");
    if (!r.ok) { $("rtsp-panel").hidden = true; return; }
    renderRtsp(await r.json());
  } catch (e) {
    $("rtsp-panel").hidden = true;
  }
}

async function toggleRtsp() {
  const btn = $("btn-rtsp-toggle");
  const isRunning = $("rtsp-state").classList.contains("on");
  const path = isRunning ? "/api/rtsp/stop" : "/api/rtsp/start";
  btn.disabled = true;
  setRtspMsg(isRunning ? "stopping MediaMTX…" : "starting MediaMTX…");
  try {
    const r = await fetch(path, { method: "POST" });
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    renderRtsp(await r.json());
  } catch (e) {
    setRtspMsg(`${isRunning ? "stop" : "start"} failed — ${e.message}`, "err");
    btn.disabled = false;
  }
}

function wireRtsp() {
  $("btn-rtsp-toggle")?.addEventListener("click", toggleRtsp);
  $("btn-rtsp-copy")?.addEventListener("click", async () => {
    const url = $("rtsp-url").textContent;
    const btn = $("btn-rtsp-copy");
    try {
      await navigator.clipboard.writeText(url);
      btn.classList.add("done");
      btn.textContent = "OK";
      setTimeout(() => {
        btn.classList.remove("done");
        btn.textContent = "COPY";
      }, 1200);
    } catch (_) {
      setRtspMsg("clipboard blocked — select the URL and copy manually", "warn");
    }
  });
}

// ---------------------------------------------------------------------------
// Failed-print ("spaghetti") detection

function evidenceUrl(name) {
  return `/api/detect/evidence/${encodeURIComponent(name)}`;
}

function renderDetect(state) {
  const badge = $("detect-badge");
  const st = $("detect-state");
  badge.classList.remove("on", "warn", "err");
  if (state.error) {
    badge.textContent = "ERROR";
    badge.classList.add("err");
    st.textContent = "OFFLINE";
    setDetectMsg(state.error, "err");
  } else {
    badge.textContent = state.backend || "—";
    badge.classList.add(state.backend === "edgetpu" ? "on" : "warn");
    st.textContent = state.processing ? "WATCHING" : "IDLE";
    setDetectMsg("");
  }

  const w = state.window || {};
  $("detect-window").textContent =
    `${w.positives ?? 0}/${w.size ?? "—"} frames positive · needs ${w.needed ?? "—"}`;

  $("detect-led").hidden = !state.processing;
  $("detect-model").textContent = (state.model || "—").split("/").pop();
  $("detect-model").title = state.model || "";
  $("detect-threshold").textContent = state.threshold ?? "—";

  // Arming buttons (arm requires server-side control).
  $("detect-arm").hidden = !state.control_allowed;
  for (const b of document.querySelectorAll(".detect-action-btn")) {
    const active = b.dataset.action === state.action;
    b.classList.toggle("on", active);
    b.setAttribute("aria-pressed", String(active));
  }


  const ev = state.last_event;
  const card = $("detect-event");
  if (ev) {
    card.hidden = false;
    $("detect-event-score").textContent = `${Math.round(ev.score * 100)}%`;
    $("detect-event-label").textContent = ev.label;
    $("detect-event-when").textContent = ev.when;
    const name = (ev.evidence || "").split("/").pop();
    const link = $("detect-event-link");
    link.hidden = !name;
    if (name) {
      link.href = evidenceUrl(name);
      $("detect-event-thumb").src = evidenceUrl(name);
    }
  } else {
    card.hidden = true;
  }

  const strip = $("detect-strip");
  const recent = state.recent_evidence || [];
  if (recent.length) {
    strip.hidden = false;
    strip.replaceChildren(
      ...recent.map((name) => {
        const a = document.createElement("a");
        a.href = evidenceUrl(name);
        a.target = "_blank";
        a.rel = "noopener";
        a.title = name;
        const img = document.createElement("img");
        img.src = evidenceUrl(name);
        img.alt = name;
        img.loading = "lazy";
        a.appendChild(img);
        return a;
      }),
    );
  } else {
    strip.hidden = true;
    strip.replaceChildren();
  }
}

async function loadDetect() {
  try {
    const r = await fetch("/api/detect");
    if (r.status === 404) { $("detect-panel").hidden = true; return; }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    renderDetect(await r.json());
    $("detect-panel").hidden = false;
  } catch (_) {
    // transient server hiccup — keep whatever the panel last showed
  }
}

function setDetectMsg(text, kind) {
  const el = $("detect-msg");
  el.textContent = text;
  el.classList.remove("ok", "warn", "err");
  if (kind) el.classList.add(kind);
}

async function postDetect(path, body) {
  try {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`;
      setDetectMsg(detail, "err");
      return;
    }
    renderDetect(await r.json());
    setDetectMsg("");
  } catch (e) {
    setDetectMsg(String(e), "err");
  }
}

function wireDetect() {
  for (const b of document.querySelectorAll(".detect-action-btn")) {
    b.addEventListener("click", () => {
      const action = b.dataset.action;
      if (action === "pause" && !confirm("Arm PAUSE? A confirmed detection will pause the print.")) return;
      if (action === "stop" && !confirm("Arm STOP? A confirmed detection will stop the print.")) return;
      postDetect("/api/detect/action", { action });
    });
  }
}

// ---------------------------------------------------------------------------
// Chamber heating (Shelly) — armed at print start by bed target or filename
// marker; while armed the heater follows the chamber hysteresis. The settings
// are only visible when the master checkbox is on (and editable with
// control); they persist in data/heating.json and win over .conf defaults.

let heatingControlEnabled = false;
let heatingDirty = false;
let heatingOpen = false;
let heatingEnabledNow = false;
// The armed session's graph data (samples + heater phases) comes from the
// server (/api/heating/history, persisted in data/heating-session.json) —
// identical on every device. Refreshed every 30 s; the live point comes
// from the fast /api/heating poll.
let heatingSession = null;

function updateHeaterGraph(state) {
  const canvas = $("heater-graph");
  if (!canvas) return;
  // The graph spans the armed session: left = print start (session arm),
  // right = the newest sample. After the print ends it freezes and stays
  // visible until the next armed session replaces it.
  canvas.hidden = !(state.enabled && heatingSession);
  if (!state.enabled || !heatingSession) return;

  const s = state.settings || {};
  const half = (s.chamber_hysteresis ?? 5) / 2;
  const target = state.target ?? s.chamber_target ?? null;
  const bandLo = target != null ? target - half : null;
  const bandHi = target != null ? target + half : null;
  const bands = [bandLo, bandHi].filter((b) => b != null);

  const w = canvas.clientWidth || 220;
  const h = canvas.clientHeight || 48;
  const dpr = window.devicePixelRatio || 1;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  // x domain: session start → now (rescaled on every render, so the curve
  // compresses as the print grows instead of scrolling away).
  const t0 = heatingSession.armed_since * 1000;  // backend sends epoch seconds
  const lastSample = heatingSession.samples.length
    ? heatingSession.samples[heatingSession.samples.length - 1][0] * 1000
    : t0;
  // While the print runs the right edge is "now" (the domain stretches);
  // after it ends the domain freezes at the last sample.
  const t1 = state.armed ? Math.max(Date.now(), lastSample) : lastSample;
  const x = (t) => ((t - t0) / Math.max(1, t1 - t0)) * (w - 2) + 1;

  let lo = Infinity;
  let hi = -Infinity;
  for (const [, v] of heatingSession.samples) {
    lo = Math.min(lo, v);
    hi = Math.max(hi, v);
  }
  if (state.chamber_temp != null) {
    lo = Math.min(lo, +state.chamber_temp);
    hi = Math.max(hi, +state.chamber_temp);
  }
  for (const b of bands) { lo = Math.min(lo, b); hi = Math.max(hi, b); }
  if (lo === Infinity || hi === -Infinity) { lo = 0; hi = 1; }
  const pad = Math.max(1, (hi - lo) * 0.15);
  lo -= pad;
  hi += pad;
  const y = (v) => h - 3 - ((v - lo) / (hi - lo)) * (h - 6);

  const css = getComputedStyle(document.documentElement);
  const colLine = (css.getPropertyValue("--amber") || "#ffb347").trim();
  const colBand = (css.getPropertyValue("--fg-faint") || "#504a42").trim();
  const colDanger = (css.getPropertyValue("--danger") || "#eb5c4e").trim();

  // Hold band: dashed min/max lines the curve must stay inside.
  ctx.lineWidth = 1;
  ctx.strokeStyle = colBand;
  ctx.setLineDash([3, 3]);
  for (const b of bands) {
    if (b < lo || b > hi) continue;
    ctx.beginPath();
    ctx.moveTo(0, y(b));
    ctx.lineTo(w, y(b));
    ctx.stroke();
  }
  ctx.setLineDash([]);

  // Heating phases: a very light transparent red over each ON span (from
  // the ON event to the OFF event, or up to the last sample while still
  // heating).
  ctx.fillStyle = "rgba(235, 92, 78, 0.18)";
  for (const [a, b] of heatingSession.phases) {
    const ta = a * 1000;
    const tb = (b != null ? b : lastSample / 1000) * 1000;
    const xa = Math.max(0, x(ta));
    const xb = Math.min(w, x(Math.max(tb, ta)));
    if (xb > xa) ctx.fillRect(xa, 0, xb - xa, h);
  }

  ctx.strokeStyle = colLine;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  let started = false;
  for (const [t, v] of heatingSession.samples) {
    const px = x(t * 1000);
    const py = y(v);
    if (!started) { ctx.moveTo(px, py); started = true; }
    else ctx.lineTo(px, py);
  }
  // Live tail: the newest server sample can be up to 30 s old — extend the
  // curve with the current temperature from the fast poll.
  if (state.chamber_temp != null) {
    const px = x(Date.now());
    const py = y(+state.chamber_temp);
    if (!started) ctx.moveTo(px, py);
    else ctx.lineTo(px, py);
  }
  ctx.stroke();

  // Minimal horizontal scale: session start (left) vs now (right).
  const fmt = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  ctx.fillStyle = colBand;
  ctx.font = "9px ui-monospace, SFMono-Regular, Menlo, monospace";
  ctx.textAlign = "left";
  ctx.fillText(fmt(t0), 2, h - 1);
  ctx.textAlign = "right";
  ctx.fillText(fmt(t1), w - 2, h - 1);
  ctx.textAlign = "left";
}

function renderHeating(state) {
  heatingEnabledNow = !!state.enabled;
  const on = state.heater_on === true;
  const s = state.settings || {};
  const heatingTarget = s.chamber_target || null;
  const half = (s.chamber_hysteresis ?? 5) / 2;
  const liveTarget = state.target ?? (state.armed ? heatingTarget : null);

  // One line — the heater itself: "Status: heating" (relay on, red) or
  // "Status: standby", with the live chamber temperature and the band it
  // holds. A tripped safety stop replaces the line with a fat red
  // warning. Disabled: no line at all — the gear (in the header) is the
  // only affordance.
  const stateLine = $("heater-state");
  if (!state.enabled) {
    stateLine.hidden = true;
    $("heater-detail").textContent = "";
  } else {
    stateLine.hidden = false;
    if (state.safety_stop) {
      const chamber = (state.chamber_temp != null) ? `${(+state.chamber_temp).toFixed(1)}°` : "—";
      $("heater-detail").innerHTML =
        `<span class="st-danger">⚠ SAFETY STOP</span> — ${state.safety_message || "heater problem"} · ${chamber}`;
    } else if (state.armed) {
      const chamber = (state.chamber_temp != null) ? `${(+state.chamber_temp).toFixed(1)}°` : "—";
      const bandArr = Array.isArray(state.band) ? state.band : null;
      const fmtBand = (v) => String(+(+v).toFixed(2));  // 40, 44.45 — exakt, ohne Nullen
      const bandTxt = bandArr
        ? ` · hold ${fmtBand(bandArr[0])}–${fmtBand(bandArr[1])}°`
        : "";
      const statusWord = on
        ? '<span class="st-heating">heating</span>'
        : '<span class="st-standby">standby</span>';
      $("heater-detail").innerHTML = `Status: ${statusWord} · ${chamber}${bandTxt}`;
    } else {
      $("heater-detail").innerHTML = "Status: standby";
    }
  }

  updateHeaterGraph(state);

  // The header dot mirrors the heater state: red while heating, gray
  // while standby (blink pattern from the detect LED). Hidden when the
  // mechanism is not regulating a print.
  const dot = $("heater-dot");
  if (state.enabled && state.armed) {
    dot.hidden = false;
    dot.classList.toggle("heating", on);
    dot.classList.toggle("standby", !on);
  } else {
    dot.hidden = true;
  }

  // The gear unfolds the settings; the On/Off buttons live inside them
  // (same look as the Light section: the current state is highlighted).
  const editable = heatingControlEnabled;
  $("heating-fields").hidden = !heatingOpen;
  $("heating-settings-toggle").hidden = false;
  $("heating-enabled-on").disabled = !heatingControlEnabled;
  $("heating-enabled-off").disabled = !heatingControlEnabled;
  $("heating-enabled-on").classList.toggle("active", !!state.enabled);
  $("heating-enabled-off").classList.toggle("active", !state.enabled);
  for (const id of ["heating-url-on", "heating-url-off", "heating-bed-temp",
                    "heating-chamber-target", "heating-chamber-hysteresis", "heating-chamber-overshoot",
                    "heating-markers", "heating-save", "heating-manual-on", "heating-manual-off"]) {
    $(id).disabled = !editable;
  }
  // Manual buttons stay usable during an armed print — a click is a
  // one-shot relay pulse and the hysteresis corrects on the next push
  // (the automation always wins). Only a tripped safety stop locks them.
  $("heating-manual-on").disabled = !editable || !!state.safety_stop;
  $("heating-manual-off").disabled = !editable || !!state.safety_stop;

  // Hydrate inputs — never while the user is typing in them.
  if (!heatingDirty) {
    $("heating-url-on").value = s.url_on || "";
    $("heating-url-off").value = s.url_off || "";
    $("heating-bed-temp").value = s.bed_temp ?? "";
    $("heating-chamber-target").value = s.chamber_target ?? "";
    $("heating-chamber-target").max = s.target_max ?? 60;  // the safety cap from the conf
    $("heating-chamber-hysteresis").value = s.chamber_hysteresis ?? "";
    $("heating-chamber-overshoot").value = s.chamber_overshoot ?? "";
    $("heating-markers").value = (s.material_markers || []).join(",");
  }
}

async function loadHeatingHistory() {
  try {
    const r = await fetch("/api/heating/history");
    if (r.status === 404) { heatingSession = null; return; }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    heatingSession = await r.json();
  } catch (_) {
    // transient hiccup — keep the last known session
  }
}

async function loadHeating() {
  try {
    const r = await fetch("/api/heating");
    if (r.status === 404) { $("heating-panel").hidden = true; return; }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    renderHeating(await r.json());
    $("heating-panel").hidden = false;
  } catch (_) {
    // transient server hiccup — keep whatever the panel last showed
  }
}

async function postHeating(body, btn) {
  const original = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    const r = await fetch("/api/heating/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`;
      if (btn) btn.textContent = "✗ failed";
      return false;
    }
    renderHeating(await r.json());
    if (btn) btn.textContent = "✓ saved";
    return true;
  } catch (e) {
    if (btn) btn.textContent = "✗ failed";
    return false;
  } finally {
    if (btn) setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1500);
  }
}

async function manualCommand(on) {
  try {
    const r = await fetch("/api/heating/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on }),
    });
    if (!r.ok) {
      const detail = (await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`;
      console.warn("manual heater command failed:", detail);
      return;
    }
    renderHeating(await r.json());
  } catch (e) {
    console.warn("manual heater command failed:", e);
  }
}

function wireHeating() {
  $("heating-settings-toggle").addEventListener("click", () => {
    heatingOpen = !heatingOpen;
    $("heating-fields").hidden = !heatingOpen;
  });
  $("heating-enabled-on").addEventListener("click", async () => {
    if (heatingEnabledNow) return;
    const ok = await postHeating({ enabled: true });
    if (!ok) $("heating-enabled-on").title = "toggle failed — see journal";
  });
  $("heating-enabled-off").addEventListener("click", async () => {
    if (!heatingEnabledNow) return;
    const ok = await postHeating({ enabled: false });
    if (!ok) $("heating-enabled-off").title = "toggle failed — see journal";
  });
  $("heating-manual-on").addEventListener("click", async () => {
    await manualCommand(true);
  });
  $("heating-manual-off").addEventListener("click", async () => {
    await manualCommand(false);
  });
  for (const id of ["heating-url-on", "heating-url-off", "heating-bed-temp",
                    "heating-chamber-target", "heating-chamber-hysteresis", "heating-chamber-overshoot",
                    "heating-markers"]) {
    $(id).addEventListener("input", () => { heatingDirty = true; });
  }
  $("heating-save").addEventListener("click", async () => {
    const btn = $("heating-save");
    const body = {
      url_on: $("heating-url-on").value.trim(),
      url_off: $("heating-url-off").value.trim(),
      bed_temp: parseFloat($("heating-bed-temp").value),
      chamber_target: parseFloat($("heating-chamber-target").value),
      chamber_hysteresis: parseFloat($("heating-chamber-hysteresis").value),
      chamber_overshoot: parseFloat($("heating-chamber-overshoot").value),
      material_markers: $("heating-markers").value,
    };
    const ok = await postHeating(body, btn);
    if (ok) heatingDirty = false;
  });
}

// ---------------------------------------------------------------------------
// Canvas multi-filament (CC2 only)

let canvasControlEnabled = false;

function setCanvasMsg(text, kind) {
  const el = $("canvas-msg");
  el.classList.remove("ok", "warn", "err");
  if (kind) el.classList.add(kind);
  el.textContent = text || "";
}

function renderCanvas(data) {
  const panel = $("canvas-panel");
  // Firmware 01.03.02.51 wraps the payload in canvas_info; tolerate a
  // flat shape too (CanvasStatus.from_payload accepts both).
  const ci = data && (data.canvas_info || (data.canvas_list ? data : null));
  const hasTrays = !!(ci && (ci.canvas_list || []).some((u) => (u.tray_list || []).length));
  if (!ci || !hasTrays) { panel.hidden = true; return; }  // CC1: leere Liste → Panel aus
  panel.hidden = false;
  const traysEl = $("canvas-trays");
  traysEl.innerHTML = "";

  for (const unit of (ci.canvas_list || [])) {
    for (const t of (unit.tray_list || [])) {
      const loaded = t.status === 1;
      const active = t.tray_id === ci.active_tray_id;
      const row = document.createElement("div");
      row.className = "canvas-tray" + (active ? " active" : "") + (loaded ? "" : " empty");
      const swatch = document.createElement("span");
      swatch.className = "canvas-swatch";
      swatch.style.background = t.filament_color;
      const name = document.createElement("span");
      name.className = "canvas-tray-name";
      name.textContent = t.filament_name;
      const type = document.createElement("span");
      type.className = "canvas-tray-type";
      type.textContent = t.filament_type;
      const temp = document.createElement("span");
      temp.className = "canvas-tray-temp";
      temp.textContent = `${t.min_nozzle_temp}-${t.max_nozzle_temp}°`;
      const stat = document.createElement("span");
      stat.className = "canvas-tray-status";
      stat.textContent = loaded ? "●" : "○";
      row.append(swatch, name, type, temp, stat);
      traysEl.appendChild(row);
    }
  }

  $("canvas-refill-state").textContent = ci.auto_refill ? "ON" : "OFF";
  $("canvas-refill-state").classList.toggle("on", ci.auto_refill);

  const toggleBtn = $("canvas-refill-toggle");
  if (canvasControlEnabled) {
    toggleBtn.hidden = false;
    $("canvas-refill-btn-label").textContent = ci.auto_refill ? "DISABLE" : "ENABLE";
  }
}

let canvasSupported = true;
let canvasRetryTimer = null;

async function loadCanvas() {
  // 501 means "this printer has no Canvas" (CC1) — stop asking. Anything
  // else (504 timeout, 503 reconnecting, network blip) is transient: keep
  // whatever is on screen and retry, so a CC2 that was mid-cooldown at
  // page load doesn't lose its Canvas panel until a manual reload.
  if (!canvasSupported) return;
  if (canvasRetryTimer) { clearTimeout(canvasRetryTimer); canvasRetryTimer = null; }
  try {
    const r = await fetch("/canvas");
    if (r.status === 501) {
      canvasSupported = false;
      $("canvas-panel").hidden = true;
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    renderCanvas(await r.json());
  } catch (_) {
    canvasRetryTimer = setTimeout(loadCanvas, 30000);
  }
}

async function toggleRefill() {
  const btn = $("canvas-refill-toggle");
  const isOn = $("canvas-refill-state").textContent === "ON";
  btn.disabled = true;
  setCanvasMsg(isOn ? "disabling auto-refill…" : "enabling auto-refill…");
  try {
    const r = await fetch("/canvas/refill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !isOn }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    setCanvasMsg(`auto-refill ${isOn ? "disabled" : "enabled"}`, "ok");
    setTimeout(loadCanvas, 2000);
  } catch (e) {
    setCanvasMsg(`toggle failed — ${e.message}`, "err");
  } finally {
    setTimeout(() => { btn.disabled = false; }, 1500);
  }
}

function wireCanvas() {
  $("canvas-refresh")?.addEventListener("click", () => {
    setCanvasMsg("refreshing…");
    loadCanvas().then(() => setCanvasMsg(""));
  });
  $("canvas-refill-toggle")?.addEventListener("click", toggleRefill);
}

// ---------------------------------------------------------------------------
// Files (GET /files, GET /disk, POST /files/delete). /disk is CC2-only;
// its 501 on the CC1 is swallowed (best-effort disk-free display).

let filesControlEnabled = false;
let filesSupported = true;

function setFilesMsg(text, kind) {
  const el = $("files-msg");
  el.classList.remove("ok", "warn", "err");
  if (kind) el.classList.add(kind);
  el.textContent = text || "";
}

function fmtSize(bytes) {
  if (bytes == null) return "";
  if (bytes >= 1e6) return `${(bytes / 1048576).toFixed(1)} MB`;
  if (bytes >= 1e3) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

function renderFiles(data) {
  const listEl = $("files-list");
  listEl.innerHTML = "";
  const files = (data && data.file_list) || [];
  if (!files.length) {
    const empty = document.createElement("div");
    empty.className = "files-empty";
    empty.textContent = "(no files)";
    listEl.appendChild(empty);
    return;
  }
  const storage = $("files-storage").value;
  for (const f of files) {
    const row = document.createElement("div");
    row.className = "files-row";
    const name = document.createElement("span");
    name.className = "files-name";
    name.textContent = f.filename;
    name.title = f.filename;
    const meta = document.createElement("span");
    meta.className = "files-size";
    const bits = [fmtSize(f.size)];
    if (f.layer) bits.push(`${f.layer}L`);
    meta.textContent = bits.join(" · ");
    row.append(name, meta);
    if (filesControlEnabled) {
      row.appendChild(reprintButton(f.filename, storage));
      const del = document.createElement("button");
      del.className = "btn files-del";
      del.type = "button";
      del.textContent = "✕";
      del.title = "Delete";
      del.addEventListener("click", () => deleteFile(f.filename, storage));
      row.appendChild(del);
    }
    listEl.appendChild(row);
  }
}

async function loadFiles() {
  // 501 = this printer has no file API (CC1); stop asking.
  if (!filesSupported) return;
  const storage = $("files-storage").value;
  try {
    const r = await fetch(`/files?storage=${encodeURIComponent(storage)}&limit=200`);
    if (r.status === 501) {
      filesSupported = false;
      $("files-panel").hidden = true;
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    $("files-panel").hidden = false;
    renderFiles(await r.json());
  } catch (_) {
    // transient (504 cooldown / reconnect) — keep what's on screen.
    return;
  }
  // Disk usage in the header (best-effort).
  try {
    const d = await (await fetch("/disk")).json();
    if (d && d.total_bytes) {
      const freeGb = (d.total_bytes - d.used_bytes) / 1e9;
      $("files-disk").textContent = `${freeGb.toFixed(1)} GB free`;
    }
  } catch (_) { /* ignore */ }
}

// Printer is idle enough to start a new job: no active print (0 = IDLE), or
// a finished/aborted one still lingering in the status (8 = STOPPED, 9 =
// COMPLETED). Null = no status received yet (fresh page, printer silent).
function printerIdle() {
  return lastPrintStatus == null || [0, 8, 9].includes(lastPrintStatus);
}

function reprintButton(name, storage) {
  const btn = document.createElement("button");
  btn.className = "btn files-del files-print";
  btn.type = "button";
  btn.textContent = "▶";
  btn.title = "Print again";
  btn.hidden = !printerIdle();
  btn.addEventListener("click", () => reprintFile(name, storage));
  return btn;
}

// Show/hide every ▶ button as the printer's state changes, so a busy
// printer simply offers no reprint instead of rejecting one.
function updateReprintVisibility() {
  const idle = printerIdle();
  for (const btn of document.querySelectorAll(".files-print")) btn.hidden = !idle;
  // Upload while a print is running makes no sense (start would collide).
  for (const el of document.querySelectorAll(".upload-disable")) {
    el.disabled = !idle;
    el.classList.toggle("disabled", !idle);
  }
}

async function reprintFile(name, storage) {
  if (!printerIdle()) {
    setFilesMsg("printer is busy — reprint is only possible while idle", "warn");
    return;
  }
  if (!confirm(`Print "${name}" again?`)) return;
  setFilesMsg(`starting ${name}…`);
  try {
    const r = await fetch("/print/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: name, storage }),
    });
    if (r.status === 504) {
      // Same as the control actions: the command reached the printer but
      // confirmation didn't arrive in time — almost always it still starts.
      setFilesMsg(`⧗ start sent for ${name} — no confirmation yet; watch the status`, "warn");
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    setFilesMsg(`✓ started ${name}`, "ok");
  } catch (e) {
    setFilesMsg(`start failed — ${e.message}`, "err");
  }
}

async function deleteFile(name, storage) {
  if (!confirm(`Delete "${name}"?\n\nThis cannot be undone.`)) return;
  setFilesMsg(`deleting ${name}…`);
  try {
    const r = await fetch("/files/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filenames: [name], storage }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}: ${await r.text()}`);
    setFilesMsg(`deleted ${name}`, "ok");
    setTimeout(loadFiles, 1500);
  } catch (e) {
    setFilesMsg(`delete failed — ${e.message}`, "err");
  }
}

function wireFiles() {
  $("files-refresh")?.addEventListener("click", () => {
    setFilesMsg("refreshing…");
    loadFiles().then(() => setFilesMsg(""));
  });
  $("files-storage")?.addEventListener("change", () => loadFiles());
}

// ---------------------------------------------------------------------------
// Print history (GET /history)

let historySupported = true;
// task_status enum, confirmed 2026-07-15 by correlating with the stock
// dashboard's Complete/Cancel labels: 1 = completed, 2 = cancelled.
// (The CC1's native 3 = stopped is normalised to 2 in client.py.)
const HISTORY_STATUS = { 1: "completed", 2: "cancelled" };

function fmtDate(unixSec) {
  if (!unixSec) return "";
  const d = new Date(unixSec * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function renderHistory(data) {
  const listEl = $("history-list");
  listEl.innerHTML = "";
  // The printer returns history oldest-first; show newest-first.
  const tasks = [...((data && data.history_task_list) || [])].reverse();
  if (!tasks.length) {
    const empty = document.createElement("div");
    empty.className = "files-empty";
    empty.textContent = "(no history)";
    listEl.appendChild(empty);
    return;
  }
  for (const t of tasks) {
    const row = document.createElement("div");
    row.className = "files-row";
    const name = document.createElement("span");
    name.className = "files-name";
    name.textContent = t.task_name || "?";
    name.title = t.task_name || "";
    const label = HISTORY_STATUS[t.task_status];
    if (label) {
      const stat = document.createElement("span");
      stat.className = "hist-status hist-" + label;
      stat.textContent = label;
      row.appendChild(name);
      row.appendChild(stat);
    } else {
      row.appendChild(name);
    }
    const when = document.createElement("span");
    when.className = "files-size";
    when.textContent = fmtDate(t.end_time || t.begin_time);
    row.appendChild(when);
    if (filesControlEnabled) row.appendChild(reprintButton(t.task_name || "", "local"));
    listEl.appendChild(row);
  }
}

async function loadHistory() {
  if (!historySupported) return;
  try {
    const r = await fetch("/history");
    if (r.status === 501) {
      historySupported = false;
      $("history-panel").hidden = true;
      return;
    }
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    $("history-panel").hidden = false;
    renderHistory(await r.json());
  } catch (_) {
    return; // transient — keep what's on screen
  }
}

function wireHistory() {
  $("history-refresh")?.addEventListener("click", () => loadHistory());
}

// ---------------------------------------------------------------------------
// Webcam — fetch the MJPEG stream and paint frames into the <img>.
//
// This deliberately does not point <img> at /stream. With the browser's
// native multipart <img>:
//   * Chrome fires `load` at most once per x-mixed-replace stream, so no
//     timer can tell a healthy stream from a stalled one;
//   * every "reload" abandoned the previous in-flight multipart load, and
//     browsers tear those down late or never — each abandoned stream kept
//     pulling the full stream rate, so total bandwidth climbed by one
//     stream per reload (observed: 2 MB/s → 20+ MB/s in ~10 minutes).
// Fetching ourselves gives exactly one connection per tab, a per-frame
// callback (deterministic staleness detection) and a real teardown
// (AbortController).
//
// The fetch targets /stream/raw, not /stream: WebKit (Safari) cannot
// fetch() a multipart/x-mixed-replace response at all — the body errors
// almost immediately (TypeError: Load failed) — while the same bytes as
// application/octet-stream stream fine. We parse the multipart framing
// ourselves either way; the boundary comes from X-Stream-Boundary.

const WEBCAM_STALE_MS = 15000; // mirrors the server's upstream stale timeout
const WEBCAM_MAX_FRAME_BYTES = 32 * 1024 * 1024;

function mjpegBoundary(res) {
  // /stream/raw carries the already-normalized boundary prefix here.
  const explicit = res.headers.get("x-stream-boundary");
  if (explicit) return explicit;
  const m = /boundary="?([^";]+)"?/i.exec(res.headers.get("content-type") || "");
  // The CC1 declares `boundary=--foo` — dashes already included — but emits
  // plain `--foo` lines, so strip leading dashes before prepending our own.
  return "--" + (m ? m[1] : "frame").replace(/^-+/, "");
}

// Byte-sequence search. Naive is fine at MJPEG rates (~2 MB/s).
function indexOfSeq(buf, seq, from) {
  outer: for (let i = from; i <= buf.length - seq.length; i++) {
    for (let j = 0; j < seq.length; j++) {
      if (buf[i + j] !== seq[j]) continue outer;
    }
    return i;
  }
  return -1;
}

// Read an MJPEG multipart body, calling onFrame(Uint8Array) with each
// complete JPEG. Resolves when the body ends (caller reconnects); rejects
// on abort. Handles split chunks, missing Content-Length, and headerless
// parts. `pos` always marks the resume point inside `buf`, so any read
// split is safe.
async function pumpMjpeg(body, boundary, onFrame) {
  // `boundary` is the full delimiter line prefix, as returned by
  // mjpegBoundary() (e.g. "--foo") — already includes its leading dashes.
  const delim = new TextEncoder().encode(boundary);
  const BLANK = new Uint8Array([13, 10, 13, 10]);
  const reader = body.getReader();
  let buf = new Uint8Array(0);
  let pos = 0;
  let phase = "seek"; // seek boundary | parse headers | emit frame data
  let contentLength = null;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) return;
    // Append the new bytes, dropping everything already parsed.
    const merged = new Uint8Array(buf.length - pos + value.length);
    merged.set(buf.subarray(pos), 0);
    merged.set(value, buf.length - pos);
    buf = merged;
    pos = 0;

    let progressed = true;
    while (progressed) {
      progressed = false;
      if (phase === "seek") {
        const i = indexOfSeq(buf, delim, pos);
        if (i < 0) {
          // Bytes before the next boundary are preamble — nothing to
          // render. Keep only a possible partial delimiter at the tail.
          buf = buf.subarray(Math.max(pos, buf.length - (delim.length - 1)));
          pos = 0;
          break;
        }
        pos = i + delim.length;
        if (buf.length - pos >= 2 && buf[pos] === 45 && buf[pos + 1] === 45) return; // terminal "--"
        phase = "headers";
        progressed = true;
      } else if (phase === "headers") {
        // The boundary line's CRLF (possibly split across reads).
        if (buf.length - pos < 2) break;
        if (buf[pos] === 13 && buf[pos + 1] === 10) pos += 2;
        // A header block starts with "content-"; anything else means the
        // part has no headers and the data begins right here.
        const probe = String.fromCharCode(...buf.subarray(pos, Math.min(buf.length, pos + 7))).toLowerCase();
        if (probe.length < 7 && "content".startsWith(probe)) break; // undecided yet
        if (probe === "content") {
          const headEnd = indexOfSeq(buf, BLANK, pos);
          if (headEnd < 0) {
            if (buf.length - pos > 1024) throw new Error("oversized multipart headers");
            break; // header block still streaming in
          }
          const headers = new TextDecoder("latin1").decode(buf.subarray(pos, headEnd));
          const m = /content-length\s*:\s*(\d+)/i.exec(headers);
          contentLength = m ? parseInt(m[1], 10) : null;
          pos = headEnd + 4;
        } else {
          contentLength = null;
        }
        phase = "data";
        progressed = true;
      } else { // data
        if (contentLength != null) {
          if (buf.length < pos + contentLength) break; // frame still streaming
          onFrame(buf.subarray(pos, pos + contentLength));
          pos += contentLength;
          if (buf.length - pos >= 2 && buf[pos] === 13 && buf[pos + 1] === 10) pos += 2; // part CRLF
          phase = "seek";
          progressed = true;
        } else {
          // No Content-Length: the frame ends at the next boundary.
          const i = indexOfSeq(buf, delim, pos);
          if (i < 0) {
            if (buf.length > WEBCAM_MAX_FRAME_BYTES) throw new Error("oversized frame");
            break;
          }
          const end = i >= 2 && buf[i - 2] === 13 && buf[i - 1] === 10 ? i - 2 : i;
          onFrame(buf.subarray(pos, end));
          pos = i;
          phase = "seek";
          progressed = true;
        }
      }
    }
    if (pos > 0) {
      buf = buf.subarray(pos);
      pos = 0;
    }
  }
}

function wireWebcam() {
  const img = $("webcam");
  if (!img) return;
  let controller = null;
  let lastFrame = Date.now();
  let backoffMs = 1000;

  // A hidden tab must not keep pulling the camera stream: nobody watches
  // it, and every backgrounded tab on the LAN drains the full stream rate
  // (~2 MB/s each). visibilitychange aborts the fetch (real teardown, the
  // server drops the subscriber) and gates the reconnect loop until the
  // tab is visible again — a tab opened in the background starts with
  // zero camera traffic. SSE status updates are a few bytes per second
  // and deliberately keep flowing.
  let wake = null; // resolves when the tab becomes visible again

  function waitVisible() {
    return new Promise((resolve) => {
      if (!document.hidden) { resolve(); return; }
      wake = resolve;
    });
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      controller?.abort();
    } else if (wake) {
      const resolve = wake;
      wake = null;
      lastFrame = Date.now(); // fresh staleness window for the reconnect
      resolve();
    }
  });

  async function connect() {
    for (;;) {
      await waitVisible();
      controller = new AbortController();
      try {
        const res = await fetch("/stream/raw", { signal: controller.signal, cache: "no-store" });
        if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
        backoffMs = 1000;
        await pumpMjpeg(res.body, mjpegBoundary(res), (jpeg) => {
          lastFrame = Date.now();
          const url = URL.createObjectURL(new Blob([jpeg], { type: "image/jpeg" }));
          const previous = img.src;
          img.src = url;
          if (previous.startsWith("blob:")) URL.revokeObjectURL(previous);
        });
      } catch (_) {
        // Aborted (watchdog, hidden tab) or network error — reconnect
        // with backoff; waitVisible() holds the loop while hidden.
      }
      await new Promise((resolve) => setTimeout(resolve, backoffMs));
      backoffMs = Math.min(backoffMs * 2, 10000);
    }
  }

  // Staleness watchdog: the printer's camera can stall while the socket
  // stays open (the server guards the same failure upstream). Aborting
  // drops our one connection; the loop reconnects. No reloads, so no
  // abandoned streams to accumulate bandwidth.
  setInterval(() => {
    if (Date.now() - lastFrame > WEBCAM_STALE_MS) {
      lastFrame = Date.now();
      controller?.abort();
    }
  }, 5000);

  connect();
}

// ---------------------------------------------------------------------------

// Adaptive backup poll. SSE is the primary update path; this poll keeps
// the UI alive when SSE silently stalls (Firefox in particular). Rate
// follows the printer's state: fast while actively printing, slow when
// idle/paused/done/errored, so we're not hammering the firmware at
// 2 Hz when nothing is happening.
const IDLE_STATUSES = new Set([0, 6, 8, 9, 14]); // idle, paused, stopped, completed, error
const POLL_FAST_MS = 2000;
const POLL_SLOW_MS = 10000;
let pollTimer = null;
let lastPrintStatus = null;

function pollIntervalMs() {
  return (lastPrintStatus != null && !IDLE_STATUSES.has(lastPrintStatus))
    ? POLL_FAST_MS : POLL_SLOW_MS;
}

function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    await pollOnce();
    schedulePoll();
  }, pollIntervalMs());
}

function noteStatusForPoll(pstatus) {
  const prev = lastPrintStatus;
  lastPrintStatus = pstatus;
  updateReprintVisibility();
  const wasActive = prev != null && !IDLE_STATUSES.has(prev);
  const nowActive = pstatus != null && !IDLE_STATUSES.has(pstatus);
  if (wasActive !== nowActive) schedulePoll(); // retune immediately
}

(async function main() {
  wireControls();
  wireAdjust();
  wireCanvas();
  wireFiles();
  wireHistory();
  wireRtsp();
  wireDetect();
  wireHeating();
  wireWebcam();
  await loadInfo();
  await loadCanvas();
  await loadFiles();
  await loadHistory();
  await loadRtsp();
  await loadDetect();
  await loadHeating();
  await loadHeatingHistory();
  await pollOnce();
  connectSSE();
  setInterval(loadInfo, 60000);
  // Session history (graph): 30 s cadence, the live point comes from the
  // 5 s status/heating polls.
  setInterval(loadHeatingHistory, 30000);
  // RTSP state changes are external, poll occasionally.
  setInterval(loadRtsp, 8000);
  // Detection state too (processing flag, window, new evidence).
  setInterval(loadDetect, 5000);
  // Heating state (armed session, heater, chamber temp).
  setInterval(loadHeating, 5000);
  schedulePoll();
})();
