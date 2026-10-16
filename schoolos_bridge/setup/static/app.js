"use strict";

const DIRECTION_LABEL = { IN: "Kirish", OUT: "Chiqish", BOTH: "Kirish / chiqish" };
const SESSION_KEY = "schoolos-bridge-session";
const IPV4 = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$/;

const state = {
  token: "",
  cloudUrl: "",
  tokenSet: false,
  terminals: [],
};

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ── Session: one-time link in the URL fragment → token in this tab only ── */

function readSession() {
  try {
    return sessionStorage.getItem(SESSION_KEY) || "";
  } catch {
    return "";
  }
}

function writeSession(token) {
  try {
    if (token) sessionStorage.setItem(SESSION_KEY, token);
    else sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* private mode: the session just lasts until reload */
  }
}

function showGate(title, text) {
  $("gateTitle").textContent = title;
  $("gateText").textContent = text;
  $("gate").hidden = false;
  $("app").hidden = true;
  $("saveBar").hidden = true;
  $("topRight").hidden = true;
}

function signedOut(reason) {
  state.token = "";
  writeSession("");
  showGate(
    reason || "Kirish kerak",
    "Bridge'ni qayta oching: kompyuterda «schoolos-bridge» dasturini ishga tushiring (yoki «schoolos-bridge open»). Brauzer o'zi kirib keladi.",
  );
}

async function startSession() {
  const link = new URLSearchParams(location.hash.slice(1)).get("t");
  // Drop the link from the address bar and history at once.
  if (location.hash) history.replaceState(null, "", location.pathname);
  if (link) {
    try {
      const res = await fetch("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ link }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return signedOut(data.message ? "Havola ishlamadi" : "Kirib bo'lmadi");
      state.token = data.token || "";
      writeSession(state.token);
    } catch {
      return signedOut("Bridge javob bermadi");
    }
  } else {
    state.token = readSession();
  }
  if (!state.token) return signedOut();
  try {
    await loadState();
  } catch (err) {
    if (!state.token) return;
    showGate("Xatolik", err.message);
    return;
  }
  $("gate").hidden = true;
  $("app").hidden = false;
  $("saveBar").hidden = false;
  $("topRight").hidden = false;
  setInterval(refreshStatus, 5000);
}

/* ── API ─────────────────────────────────────────────────────────────── */

async function api(path, body) {
  let res;
  try {
    res = await fetch(path, {
      method: body ? "POST" : "GET",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.token}` },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new Error("Bridge bilan aloqa yo'q");
  }
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) {
    signedOut("Sessiya tugadi");
    throw new Error(data.message || "Sessiya tugadi");
  }
  if (!res.ok) throw new Error(data.message || "Xatolik");
  return data;
}

/** `action` = { label, run } adds a button (e.g. "Qaytarish") to the toast. */
function toast(message, kind = "ok", action = null) {
  const box = $("toast");
  $("toastText").textContent = message;
  box.className = "toast" + (kind === "err" ? " err" : "");
  box.setAttribute("role", kind === "err" ? "alert" : "status");
  const btn = $("toastAction");
  btn.hidden = !action;
  btn.onclick = null;
  if (action) {
    btn.textContent = action.label;
    btn.onclick = () => {
      box.hidden = true;
      action.run();
    };
  }
  box.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => (box.hidden = true), action ? 8000 : 4000);
}

function busy(btn, label) {
  btn.disabled = true;
  btn.dataset.label = btn.textContent;
  btn.innerHTML = `<span class="spinner" aria-hidden="true"></span> ${esc(label)}`;
}

function idle(btn) {
  btn.disabled = false;
  btn.textContent = btn.dataset.label || btn.textContent;
}

/* ── Service status ─────────────────────────────────────────────────── */

function renderService(service) {
  const pill = $("serviceState");
  const error = $("serviceError");
  if (!service) {
    pill.className = "pill";
    pill.textContent = "sozlash rejimi";
    error.hidden = true;
    return;
  }
  if (service.running) {
    pill.className = "pill ok";
    pill.textContent = `Ishlayapti · ${service.devices} terminal`;
    error.hidden = true;
  } else {
    pill.className = "pill warn";
    pill.textContent = "To'xtagan";
    error.textContent = service.error || "Bridge sozlanmagan.";
    error.hidden = false;
  }
}

async function refreshStatus() {
  if (!state.token || document.hidden) return;
  try {
    renderService((await api("/api/state")).service);
  } catch {
    /* the next poll retries; 401 already signed out */
  }
}

/* ── Terminals ───────────────────────────────────────────────────────── */

function splitHost(value) {
  const text = String(value || "");
  const at = text.lastIndexOf(":");
  if (at > 0 && text.indexOf(":") === at) {
    const port = Number(text.slice(at + 1));
    if (port > 0) return { ip: text.slice(0, at), port };
  }
  return { ip: text, port: 80 };
}

const termId = (ip, port) => `${ip}:${port || 80}`;

function upsertTerminal(fields) {
  const id = termId(fields.ip, fields.port);
  const existing = state.terminals.find((t) => t.id === id);
  if (existing) Object.assign(existing, fields, { id });
  else state.terminals.push({ id, port: 80, username: "admin", password: "", direction: "BOTH", status: "found", message: "", vendor: "hikvision", ...fields, id });
  return state.terminals.find((t) => t.id === id);
}

const STATUS = {
  found: ["parol kerak", "info"],
  configured: ["saqlangan", "ok"],
  checking: ["tekshirilmoqda", "busy"],
  ok: ["tayyor", "ok"],
  error: ["xato", "err"],
};

function termCard(t) {
  const [label, tone] = STATUS[t.status] || ["", ""];
  const cls = tone === "ok" ? "ok" : tone === "err" ? "err" : "";
  const meta = [t.model && esc(t.model), t.serial && `<code>${esc(t.serial)}</code>`].filter(Boolean).join(" · ");
  const msg = t.message ? `<p class="term-msg ${t.status === "error" ? "err" : ""}">${esc(t.message)}</p>` : "";
  const confirming = t.confirmDelete;
  return `
    <div class="term ${cls}" data-term="${esc(t.id)}">
      <div class="term-top">
        <div>
          <span class="term-ip">${esc(t.ip)}${t.port && t.port !== 80 ? ":" + t.port : ""}</span>
          ${meta ? `<span class="term-meta">${meta}</span>` : ""}
        </div>
        <span class="pill ${tone}">${esc(label)}</span>
      </div>
      <div class="term-grid">
        <label class="field"><span>Login</span>
          <input data-field="username" value="${esc(t.username)}" autocomplete="off" /></label>
        <label class="field"><span>Parol</span>
          <input data-field="password" type="password" value="${esc(t.password)}" placeholder="${t.password_set ? "saqlangan" : "admin paroli"}" autocomplete="off" /></label>
        <label class="field"><span>Joy</span>
          <input data-field="location" value="${esc(t.location || "")}" placeholder="Asosiy kirish" /></label>
        <label class="field"><span>Yo'nalish</span>
          <select data-field="direction">
            ${["BOTH", "IN", "OUT"]
              .map((d) => `<option value="${d}" ${t.direction === d ? "selected" : ""}>${DIRECTION_LABEL[d]}</option>`)
              .join("")}
          </select></label>
      </div>
      ${msg}
      <div class="term-actions">
        <button class="btn btn-secondary" data-action="probe" type="button" ${t.status === "checking" ? "disabled" : ""}>
          ${t.status === "checking" ? '<span class="spinner" aria-hidden="true"></span> Tekshirilmoqda' : "Tekshirish"}
        </button>
        <button class="btn ${confirming ? "btn-danger-solid" : "btn-danger"}" data-action="delete" type="button">
          ${confirming ? "Ha, o'chirish" : "O'chirish"}
        </button>
        ${confirming ? '<button class="btn btn-ghost" data-action="keep" type="button">Bekor</button>' : ""}
      </div>
    </div>`;
}

// Re-rendering replaces the inputs; remember where the cursor was so typing
// in one card is not interrupted when another card's probe finishes.
function captureFocus(root) {
  const el = document.activeElement;
  if (!el || !root.contains(el)) return null;
  const card = el.closest("[data-term]");
  if (!card) return null;
  let start = null;
  let end = null;
  try {
    start = el.selectionStart;
    end = el.selectionEnd;
  } catch {
    /* select / button: no caret */
  }
  return { term: card.dataset.term, field: el.dataset.field, action: el.dataset.action, start, end };
}

function restoreFocus(root, focus) {
  if (!focus) return;
  const card = root.querySelector(`[data-term="${CSS.escape(focus.term)}"]`);
  const selector = focus.field ? `[data-field="${focus.field}"]` : focus.action ? `[data-action="${focus.action}"]` : "";
  const el = card && selector ? card.querySelector(selector) : null;
  if (!el) return;
  el.focus();
  if (focus.start != null && el.setSelectionRange) {
    try {
      el.setSelectionRange(focus.start, focus.end);
    } catch {
      /* input type without a caret */
    }
  }
}

function renderTerminals() {
  const list = $("termList");
  const focus = captureFocus(list);
  if (state.terminals.length === 0) {
    list.innerHTML = `<p class="empty">Terminal yo'q. «Skanerlash»ni bosing yoki IP qo'shing.</p>`;
  } else {
    list.innerHTML = state.terminals.map(termCard).join("");
    restoreFocus(list, focus);
  }
  updateSave();
}

/** Redraw one card only, leaving the others (and their focus) untouched. */
function renderTerminal(t) {
  const list = $("termList");
  const card = list.querySelector(`[data-term="${CSS.escape(t.id)}"]`);
  if (!card) return renderTerminals();
  const focus = captureFocus(card);
  const tpl = document.createElement("template");
  tpl.innerHTML = termCard(t).trim();
  card.replaceWith(tpl.content.firstElementChild);
  restoreFocus(list, focus);
  updateSave();
}

function updateSave() {
  const verified = state.terminals.filter((t) => t.serial).length;
  const unverified = state.terminals.length - verified;
  const cloudOk = Boolean(state.cloudUrl) && (state.tokenSet || $("cloudToken").value.trim());
  $("saveBtn").disabled = !(cloudOk && verified > 0);
  // Say why Save is disabled instead of leaving a dead button.
  $("saveHint").textContent = !cloudOk
    ? "Avval bulut manzili va tokenni kiriting."
    : verified === 0
      ? "Kamida bitta terminalni «Tekshirish» bilan tasdiqlang."
      : unverified
        ? `${verified} ta terminal saqlanadi · ${unverified} tasi hali tekshirilmagan.`
        : `${verified} ta terminal saqlanadi.`;
}

/* ── Loading ──────────────────────────────────────────────────────────── */

async function loadState() {
  const data = await api("/api/state");
  state.cloudUrl = data.cloud_url || "";
  state.tokenSet = data.token_set;
  $("cloudUrl").value = state.cloudUrl;
  if (!$("scanTarget").value) $("scanTarget").value = data.default_subnet || "";
  $("tokenHint").textContent = state.tokenSet ? "(saqlangan — o'zgartirish uchun yangisini yozing)" : "";
  renderService(data.service);

  const pending = state.terminals.filter((t) => !t.serial);
  state.terminals = (data.devices || []).map((d) => {
    const { ip, port } = splitHost(d.ip);
    return {
      id: termId(ip, port),
      ip,
      port,
      username: d.username || "admin",
      password: "",
      password_set: d.password_set,
      location: d.location_label || d.location || "",
      direction: d.direction || "BOTH",
      serial: d.serial || "",
      saved: Boolean(d.serial),
      vendor: d.vendor || "hikvision",
      model: d.model || "",
      status: d.serial ? "configured" : "found",
      message: "",
    };
  });
  // Unsaved, unverified terminals typed in this session stay listed.
  for (const t of pending) {
    if (!state.terminals.some((x) => x.id === t.id)) state.terminals.push(t);
  }
  renderTerminals();
}

/* ── Actions ──────────────────────────────────────────────────────────── */

async function testCloud() {
  const btn = $("cloudTest");
  busy(btn, "Tekshirilmoqda");
  try {
    const res = await api("/api/cloud-test", { cloud_url: $("cloudUrl").value.trim(), token: $("cloudToken").value.trim() });
    const pill = $("cloudState");
    pill.hidden = false;
    if (res.ok) {
      // The server normalizes what was typed (https, no /api); show that.
      state.cloudUrl = res.cloud_url || state.cloudUrl;
      $("cloudUrl").value = state.cloudUrl;
      pill.className = "pill ok";
      pill.textContent = "ulandi";
      $("cloudNote").textContent = res.message;
    } else {
      pill.className = "pill err";
      pill.textContent = "xato";
      $("cloudNote").textContent = res.message || "Ulanmadi";
    }
  } catch (err) {
    toast(err.message, "err");
  } finally {
    idle(btn);
    updateSave();
  }
}

async function scan() {
  const btn = $("scanBtn");
  const pill = $("scanState");
  busy(btn, "Skanerlanmoqda");
  pill.hidden = false;
  pill.className = "pill busy";
  pill.textContent = "qidirilmoqda…";
  try {
    const res = await api("/api/scan", { target: $("scanTarget").value.trim() });
    let added = 0;
    for (const row of res.found || []) {
      if (state.terminals.some((t) => t.id === termId(row.ip, row.port))) continue;
      upsertTerminal({ ip: row.ip, port: row.port, status: "found" });
      added += 1;
    }
    pill.className = "pill info";
    pill.textContent = `${(res.found || []).length} ta topildi`;
    toast(added ? `${added} ta yangi terminal` : "Yangi terminal topilmadi");
  } catch (err) {
    pill.className = "pill err";
    pill.textContent = "xato";
    toast(err.message, "err");
  } finally {
    idle(btn);
    renderTerminals();
  }
}

const PROBE_ERRORS = {
  login_required: "Login yoki parol noto'g'ri",
  unreachable: "Javob bermadi — IP, kabel va tarmoqni tekshiring",
  not_a_terminal: "Bu qurilma terminal emas",
  isapi: "Terminal xatosi",
};

async function probeTerminal(t) {
  if (!t.password && !t.password_set) {
    t.status = "error";
    t.message = "Avval parolni kiriting";
    renderTerminal(t);
    $("termList").querySelector(`[data-term="${CSS.escape(t.id)}"] [data-field="password"]`)?.focus();
    return;
  }
  t.status = "checking";
  t.message = "";
  renderTerminal(t);
  try {
    const res = await api("/api/probe", { ip: t.ip, port: t.port, username: t.username, password: t.password });
    if (res.ok) {
      Object.assign(t, {
        serial: res.serial,
        model: res.model || t.model,
        status: "ok",
        message: "",
        password_set: Boolean(t.password) || t.password_set,
      });
    } else {
      t.status = "error";
      t.message = res.error === "isapi" && res.detail ? `${PROBE_ERRORS.isapi}: ${res.detail}` : PROBE_ERRORS[res.error] || "Tekshirib bo'lmadi";
    }
  } catch (err) {
    t.status = "error";
    t.message = err.message;
  }
  renderTerminal(t);
}

async function deleteTerminal(t) {
  t.confirmDelete = false;
  if (t.saved) {
    try {
      await api("/api/device-delete", { serial: t.serial, ip: t.port && t.port !== 80 ? `${t.ip}:${t.port}` : t.ip });
    } catch (err) {
      toast(err.message, "err");
      renderTerminal(t);
      return;
    }
  }
  state.terminals = state.terminals.filter((x) => x !== t);
  renderTerminals();
  toast(t.saved ? `${t.ip} o'chirildi — bridge undan endi foydalanmaydi` : `${t.ip} ro'yxatdan olindi`);
  refreshStatus();
}

async function save() {
  const btn = $("saveBtn");
  const verified = state.terminals.filter((t) => t.serial);
  const payload = {
    cloud_url: $("cloudUrl").value.trim(),
    token: $("cloudToken").value.trim(),
    devices: verified.map((t) => ({
      serial: t.serial,
      vendor: t.vendor || "hikvision",
      model: t.model || "",
      ip: t.port && t.port !== 80 ? `${t.ip}:${t.port}` : t.ip,
      username: t.username || "admin",
      password: t.password || "",
      direction: t.direction || "BOTH",
      location_label: t.location || "",
    })),
  };
  busy(btn, "Saqlanmoqda");
  try {
    const res = await api("/api/save", payload);
    $("cloudToken").value = "";
    const reg = res.registration || {};
    if (reg.error && reg.error !== "not_configured") {
      toast(`Saqlandi, lekin bulutga yuborilmadi: ${reg.message || reg.error}`, "err");
    } else {
      toast("Saqlandi — bridge yangi sozlama bilan ishlayapti");
    }
    await loadState();
    await check();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    idle(btn);
    updateSave();
  }
}

async function check() {
  const box = $("checks");
  try {
    const res = await api("/api/check", {});
    const results = res.results || [];
    const failed = results.filter((r) => r.status === "FAIL").length;
    box.hidden = false;
    box.innerHTML =
      `<h3>${failed ? `${failed} ta muammo topildi` : "Hammasi joyida"}</h3>` +
      results
        .map(
          (r) => `<div class="check ${esc(r.status)}"><span class="st">${esc(r.status)}</span>
            <div><div>${esc(r.name)}</div><div class="detail">${esc(r.detail)}</div></div></div>`,
        )
        .join("");
  } catch (err) {
    toast(err.message, "err");
  }
}

/* ── Manual add ───────────────────────────────────────────────────────── */

function setManualError(message) {
  $("manualErr").textContent = message;
  $("manualErr").hidden = !message;
  $("manualIp").setAttribute("aria-invalid", message ? "true" : "false");
}

function toggleManual(open) {
  $("manualRow").hidden = !open;
  $("addManual").setAttribute("aria-expanded", String(open));
  setManualError("");
  if (open) {
    $("manualIp").value = "";
    $("manualIp").focus();
  }
}

function addManual(e) {
  e.preventDefault();
  const { ip, port } = splitHost($("manualIp").value.trim());
  if (!IPV4.test(ip) || !(port > 0 && port < 65536)) {
    setManualError("IP manzil noto'g'ri. Masalan: 192.168.1.64");
    $("manualIp").focus();
    return;
  }
  if (state.terminals.some((t) => t.id === termId(ip, port))) {
    setManualError("Bu terminal ro'yxatda bor.");
    $("manualIp").focus();
    return;
  }
  const t = upsertTerminal({ ip, port, status: "found" });
  toggleManual(false);
  renderTerminals();
  // Next thing to type is this terminal's password.
  $("termList").querySelector(`[data-term="${CSS.escape(t.id)}"] [data-field="password"]`)?.focus();
}

/* ── Wiring ───────────────────────────────────────────────────────────── */

$("logoutBtn").addEventListener("click", async () => {
  try {
    await api("/api/logout", {});
  } catch {
    /* signing out locally is enough */
  }
  signedOut("Chiqdingiz");
});
$("cloudUrl").addEventListener("input", (e) => {
  state.cloudUrl = e.target.value.trim();
  updateSave();
});
$("cloudToken").addEventListener("input", updateSave);
$("cloudTest").addEventListener("click", testCloud);
$("scanBtn").addEventListener("click", scan);
$("addManual").addEventListener("click", () => toggleManual($("manualRow").hidden));
$("manualRow").addEventListener("submit", addManual);
$("manualCancel").addEventListener("click", () => {
  toggleManual(false);
  $("addManual").focus();
});
$("manualIp").addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("manualCancel").click();
});
$("saveBtn").addEventListener("click", save);

$("termList").addEventListener("input", (e) => {
  const card = e.target.closest("[data-term]");
  const t = card && state.terminals.find((x) => x.id === card.dataset.term);
  const field = e.target.dataset.field;
  if (t && field) t[field] = e.target.value;
});

$("termList").addEventListener("click", (e) => {
  const card = e.target.closest("[data-term]");
  const t = card && state.terminals.find((x) => x.id === card.dataset.term);
  const action = e.target.closest("[data-action]")?.dataset.action;
  if (!t || !action) return;
  if (action === "probe") probeTerminal(t);
  else if (action === "delete") {
    if (t.confirmDelete) deleteTerminal(t);
    else {
      t.confirmDelete = true;
      renderTerminal(t);
    }
  } else if (action === "keep") {
    t.confirmDelete = false;
    renderTerminal(t);
  }
});

startSession();
