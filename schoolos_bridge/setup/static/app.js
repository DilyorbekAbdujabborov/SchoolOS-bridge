"use strict";

const DIRECTION_LABEL = { IN: "Kirish", OUT: "Chiqish", BOTH: "Kirish / chiqish" };
const PIN_KEY = "schoolos-setup-pin";

const state = {
  pin: localStorage.getItem(PIN_KEY) || "",
  authRequired: false,
  cloudUrl: "",
  tokenSet: false,
  defaultSubnet: "",
  hostname: "",
  configPath: "",
  terminals: [],
  saved: false,
};

const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(message, kind = "ok") {
  const box = $("toast");
  box.textContent = message;
  box.className = "toast" + (kind === "err" ? " err" : "");
  box.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => (box.hidden = true), 4000);
}

async function api(path, body) {
  let res;
  try {
    res = await fetch(path, {
      method: body ? "POST" : "GET",
      headers: { "Content-Type": "application/json", "X-Setup-Pin": state.pin },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (err) {
    throw new Error("Serverga ulanib bo'lmadi");
  }
  if (res.status === 401) {
    showPin(true);
    throw new Error("PIN noto'g'ri — yuqorida kiriting");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || "Xatolik");
  return data;
}

function showPin(bad) {
  const box = $("pinBox");
  if (!box.hidden && !bad) return;
  box.hidden = false;
  if (bad) toast("PIN noto'g'ri", "err");
}

/* ── Terminal helpers ─────────────────────────────────────────────── */

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

function statusPill(t) {
  const map = {
    found: ["topildi", "info"],
    configured: ["sozlangan", ""],
    checking: ["tekshirilmoqda", "busy"],
    ok: ["ulandi", "ok"],
    error: ["xato", "err"],
  };
  const [label, cls] = map[t.status] || ["", ""];
  return `<span class="pill ${cls}">${esc(label)}</span>`;
}

function renderTerminals() {
  const list = $("termList");
  if (state.terminals.length === 0) {
    list.innerHTML = `<p class="empty">Hozircha terminal yo'q. «Tarmoqni skanerlash»ni bosing yoki IP qo'shing.</p>`;
    return;
  }
  list.innerHTML = state.terminals
    .map((t) => {
      const cls = t.status === "ok" || t.status === "configured" ? "ok" : t.status === "error" ? "err" : "";
      const meta = [
        t.serial && `<span><b>Seriya</b> <code>${esc(t.serial)}</code></span>`,
        t.model && `<span><b>Model</b> ${esc(t.model)}</span>`,
        t.firmware && `<span><b>Firmware</b> ${esc(t.firmware)}</span>`,
        t.time_zone && `<span><b>Vaqt zonasi</b> <code>${esc(t.time_zone)}</code></span>`,
      ]
        .filter(Boolean)
        .join("");
      const msg = t.message ? `<p class="term-msg ${t.status === "error" ? "err" : "ok"}">${esc(t.message)}</p>` : "";
      return `
        <div class="term ${cls}" data-term="${esc(t.id)}">
          <div class="term-top">
            <span class="term-ip">${esc(t.ip)}${t.port && t.port !== 80 ? ":" + t.port : ""}</span>
            ${statusPill(t)}
          </div>
          ${meta ? `<div class="term-meta">${meta}</div>` : ""}
          <div class="term-grid">
            <label class="field"><span>Foydalanuvchi</span>
              <input data-field="username" value="${esc(t.username)}" autocomplete="off" /></label>
            <label class="field"><span>Parol${t.password_set && !t.password ? " (saqlangan)" : ""}</span>
              <input data-field="password" type="password" value="${esc(t.password)}" placeholder="${t.password_set ? "•••• (o'zgarmaydi)" : "admin paroli"}" autocomplete="off" /></label>
            <label class="field"><span>Joylashuv</span>
              <input data-field="location" value="${esc(t.location || "")}" placeholder="Asosiy kirish" /></label>
            <label class="field"><span>Yo'nalish</span>
              <select data-field="direction">
                ${["BOTH", "IN", "OUT"]
                  .map((d) => `<option value="${d}" ${t.direction === d ? "selected" : ""}>${DIRECTION_LABEL[d]}</option>`)
                  .join("")}
              </select></label>
            <button class="btn btn-secondary" data-action="probe" ${t.status === "checking" ? "disabled" : ""}>
              ${t.status === "checking" ? '<span class="spinner"></span> Tekshirilmoqda' : "Tekshirish"}
            </button>
          </div>
          ${msg}
          <div><button class="btn btn-danger btn-sm" data-action="remove" type="button">Ro'yxatdan olib tashlash</button></div>
        </div>`;
    })
    .join("");
  updateSteps();
}

/* ── Steps / save enablement ──────────────────────────────────────── */

function updateSteps() {
  const verified = state.terminals.filter((t) => t.serial).length;
  const cloudOk = Boolean(state.cloudUrl);
  const set = (name, done, active) => {
    const chip = document.querySelector(`.step-chip[data-step="${name}"]`);
    if (chip) chip.className = "step-chip" + (done ? " done" : "") + (active ? " active" : "");
  };
  set("cloud", cloudOk, !cloudOk);
  set("terminals", verified > 0, cloudOk && verified === 0);
  set("save", state.saved, cloudOk && verified > 0 && !state.saved);

  const ready = cloudOk && verified > 0;
  $("saveBtn").disabled = !ready;
  $("checkBtn").disabled = !state.saved;
}

/* ── Loading ──────────────────────────────────────────────────────── */

async function loadState() {
  const data = await api("/api/state");
  state.authRequired = data.auth_required;
  state.cloudUrl = data.cloud_url || "";
  state.tokenSet = data.token_set;
  state.defaultSubnet = data.default_subnet || "";
  state.hostname = data.hostname || "";
  state.configPath = data.config_path || "";

  $("cloudUrl").value = state.cloudUrl;
  $("scanTarget").value = state.defaultSubnet;
  $("tokenHint").textContent = state.tokenSet ? "(bo'sh qoldirsangiz o'zgarmaydi)" : "";

  $("facts").innerHTML = [
    state.hostname && `<div><dt>Kompyuter</dt><dd>${esc(state.hostname)}</dd></div>`,
    data.primary_ip && `<div><dt>IP</dt><dd>${esc(data.primary_ip)}</dd></div>`,
    state.configPath && `<div><dt>Sozlama</dt><dd>${esc(state.configPath)}</dd></div>`,
    `<div><dt>Port</dt><dd>${esc(data.listen_port)}</dd></div>`,
  ]
    .filter(Boolean)
    .join("");

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
      vendor: d.vendor || "hikvision",
      model: d.model || "",
      status: d.serial ? "configured" : "found",
      message: d.serial ? "Sozlamadan yuklandi" : "",
    };
  });
  if (state.authRequired && !state.pin) showPin();
  renderTerminals();
}

/* ── Actions ──────────────────────────────────────────────────────── */

async function testCloud() {
  const btn = $("cloudTest");
  const url = $("cloudUrl").value.trim();
  const token = $("cloudToken").value.trim();
  if (!url) return toast("Bulut manzilini kiriting", "err");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Tekshirilmoqda';
  try {
    const res = await api("/api/cloud-test", { cloud_url: url, token });
    if (res.ok) {
      state.cloudUrl = url;
      $("cloudState").className = "pill ok";
      $("cloudState").textContent = "ulandi";
      $("cloudNote").textContent = res.message;
      toast("Bulut bilan aloqa bor");
    } else {
      $("cloudState").className = "pill err";
      $("cloudState").textContent = "xato";
      $("cloudNote").textContent = res.message || "Ulanmadi";
      toast(res.message || "Ulanmadi", "err");
    }
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "Aloqani tekshirish";
    updateSteps();
  }
}

async function scan() {
  const btn = $("scanBtn");
  const target = $("scanTarget").value.trim();
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Skanerlanmoqda';
  $("scanState").hidden = false;
  $("scanState").className = "pill busy";
  $("scanState").textContent = "skanerlanmoqda…";
  try {
    const res = await api("/api/scan", { target });
    let added = 0;
    for (const row of res.found || []) {
      const existing = state.terminals.find((t) => t.id === termId(row.ip, row.port));
      if (existing) continue;
      upsertTerminal({
        ip: row.ip,
        port: row.port,
        status: "found",
        message: row.configured ? "Sozlash faylida bor — parolni tekshiring" : "",
      });
      added += 1;
    }
    $("scanState").className = "pill info";
    $("scanState").textContent = `${(res.found || []).length} ta topildi · ${added} ta yangi`;
    toast(added ? `${added} ta terminal qo'shildi` : "Yangi terminal topilmadi");
  } catch (err) {
    $("scanState").className = "pill err";
    $("scanState").textContent = "xato";
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "Tarmoqni skanerlash";
    renderTerminals();
  }
}

const PROBE_ERRORS = {
  login_required: "Login yoki parol noto'g'ri (401)",
  unreachable: "Javob bermadi — IP, kabel va tarmoqni tekshiring",
  not_a_terminal: "Bu qurilma terminal emas",
  isapi: "ISAPI xatosi",
};

async function probeTerminal(t) {
  if (!t.password && !t.password_set) return toast("Avval parolni kiriting", "err");
  t.status = "checking";
  t.message = "";
  renderTerminals();
  try {
    const res = await api("/api/probe", {
      ip: t.ip,
      port: t.port,
      username: t.username,
      password: t.password,
    });
    if (res.ok) {
      t.serial = res.serial;
      t.model = res.model || t.model;
      t.firmware = res.firmware || t.firmware;
      t.time_zone = res.time_zone || t.time_zone;
      t.status = "ok";
      t.message = "Ulandi va parol to'g'ri";
      t.password_set = Boolean(t.password) || t.password_set;
    } else {
      t.status = "error";
      t.message =
        res.error === "isapi" && res.detail
          ? `${PROBE_ERRORS.isapi}: ${res.detail}`
          : PROBE_ERRORS[res.error] || "Tekshirib bo'lmadi";
    }
  } catch (err) {
    t.status = "error";
    t.message = err.message;
  }
  renderTerminals();
}

async function save() {
  const btn = $("saveBtn");
  const verified = state.terminals.filter((t) => t.serial);
  if (verified.length === 0) return toast("Avval kamida bitta terminalni tekshiring", "err");
  const skipped = state.terminals.length - verified.length;

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

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Saqlanmoqda';
  try {
    const res = await api("/api/save", payload);
    state.saved = true;
    state.cloudUrl = payload.cloud_url;
    $("saveState").hidden = false;
    $("saveState").className = "pill ok";
    $("saveState").textContent = "saqlandi";
    $("nextSteps").hidden = false;
    const reg = res.registration || {};
    if (reg.ok) {
      toast(`Saqlandi — ${reg.count} terminal bulutga yuborildi, sinxronlash boshlandi`);
    } else if (reg.error && reg.error !== "not_configured") {
      toast(`Saqlandi, lekin bulutga qo'shilmadi: ${reg.message || reg.error}`, "err");
    } else {
      toast(skipped ? `Saqlandi (${skipped} ta tekshirilmagan o'tkazib yuborildi)` : "Sozlama saqlandi");
    }
    // Reflect saved state then run checks.
    await loadState();
    await check();
  } catch (err) {
    $("saveState").hidden = false;
    $("saveState").className = "pill err";
    $("saveState").textContent = "xato";
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "Sozlamani saqlash";
    renderTerminals();
  }
}

async function check() {
  const btn = $("checkBtn");
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Tekshirilmoqda';
  const box = $("checks");
  try {
    const res = await api("/api/check", {});
    const results = res.results || [];
    box.hidden = false;
    box.innerHTML = results
      .map(
        (r) => `<div class="check ${esc(r.status)}"><span class="st">${esc(r.status)}</span>
          <div><div>${esc(r.name)}</div><div class="detail">${esc(r.detail)}</div></div></div>`,
      )
      .join("");
    const failed = results.filter((r) => r.status === "FAIL").length;
    $("saveState").className = "pill " + (failed ? "err" : "ok");
    $("saveState").textContent = failed ? `${failed} xato` : "tayyor";
    if (failed) toast(`${failed} ta tekshiruv muvaffaqiyatsiz`, "err");
    else toast("Barcha tekshiruvlar muvaffaqiyatli");
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "Tekshiruvni ishga tushirish";
  }
}

function addManual() {
  const input = window.prompt("Terminal IP manzili (masalan 192.168.1.64)", "");
  if (!input) return;
  const { ip, port } = splitHost(input.trim());
  upsertTerminal({ ip, port, status: "found", message: "Qo'lda qo'shildi" });
  renderTerminals();
}

/* ── Wiring ───────────────────────────────────────────────────────── */

$("pinSave").addEventListener("click", () => {
  state.pin = $("pin").value.trim();
  localStorage.setItem(PIN_KEY, state.pin);
  $("pinBox").hidden = true;
  loadState().catch((err) => toast(err.message, "err"));
});
$("pin").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("pinSave").click();
});
$("cloudUrl").addEventListener("input", (e) => {
  state.cloudUrl = e.target.value.trim();
  updateSteps();
});
$("cloudTest").addEventListener("click", testCloud);
$("scanBtn").addEventListener("click", scan);
$("addManual").addEventListener("click", addManual);
$("saveBtn").addEventListener("click", save);
$("checkBtn").addEventListener("click", check);

$("termList").addEventListener("input", (e) => {
  const card = e.target.closest("[data-term]");
  if (!card) return;
  const t = state.terminals.find((x) => x.id === card.dataset.term);
  const field = e.target.dataset.field;
  if (t && field) t[field] = e.target.value;
});

$("termList").addEventListener("click", (e) => {
  const card = e.target.closest("[data-term]");
  if (!card) return;
  const t = state.terminals.find((x) => x.id === card.dataset.term);
  if (!t) return;
  const action = e.target.closest("[data-action]")?.dataset.action;
  if (action === "probe") probeTerminal(t);
  else if (action === "remove") {
    state.terminals = state.terminals.filter((x) => x.id !== t.id);
    renderTerminals();
  }
});

loadState().catch((err) => {
  if (!/PIN/.test(err.message)) toast(err.message, "err");
});
