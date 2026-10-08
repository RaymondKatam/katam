"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const state = { me: null, nodes: [], vms: [], tasks: [], catalog: null, filter: "", project: "", tab: "vms" };
const gb = (mb) => (mb / 1024).toFixed(mb % 1024 ? 1 : 0);
const when = (t) => (t ? new Date(t * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "Never");
const size = (b) => (b > 2 ** 30 ? `${(b / 2 ** 30).toFixed(1)} GB` : `${Math.max(1, Math.round(b / 2 ** 20))} MB`);

function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) e.append(kid);
  return e;
}
const btn = (label, fn, cls = "small") => el("button", { class: cls, type: "button", onclick: fn }, label);

async function api(path, opts = {}) {
  const headers = { "X-Katam": "1", ...(opts.headers || {}) };
  if (opts.body && !(opts.body instanceof FormData) && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { credentials: "same-origin", ...opts, headers });
  if (res.status === 401 && !path.startsWith("/auth/")) { showLogin(); throw new Error("Sign in required"); }
  const type = res.headers.get("Content-Type") || "";
  const body = type.includes("json") ? await res.json().catch(() => ({})) : await res.text();
  if (!res.ok) throw new Error((body && body.error) || `Request failed (${res.status})`);
  return body;
}
const post = (path, body, method = "POST") => api(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });

/* ---------- sign-in ---------- */
async function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  try {
    const c = await api("/auth/config");
    $("#sso").hidden = !c.saml;
    $("#sso").textContent = c.saml_label;
    $("#login-hint").hidden = !c.ldap;
  } catch { /* still show the form */ }
  $("#form-login [name=username]").focus();
}

$("#form-login").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const err = $("#login-error");
  err.hidden = true;
  try {
    await post("/auth/login", { username: f.username.value, password: f.password.value });
    f.password.value = "";
    start();
  } catch (e) { err.textContent = e.message; err.hidden = false; }
});

$("#btn-logout").addEventListener("click", async () => { await post("/auth/logout").catch(() => {}); location.reload(); });

async function start() {
  try { state.me = await api("/auth/me"); } catch { showLogin(); return; }
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#whoami").textContent = `${state.me.user}${state.me.admin ? " (admin)" : ""}`;
  $("#access-admin").hidden = !state.me.admin;
  state.catalog = await api("/api/v1/catalog");
  const pf = $("#project-filter");
  pf.replaceChildren(el("option", { value: "" }, "All projects"), ...state.catalog.projects.map((p) => el("option", { value: p }, p)));
  pf.value = state.project;
  refresh();
  schedule();
  if (state.me.admin) checkUpdates();
}

async function checkUpdates() {
  try {
    const u = await api("/api/v1/updates");
    if (!u.available) return;
    $("#update-text").textContent = u.blocked
      ? `Katam ${u.latest} is available (you run ${u.current}). ${u.blocked_reason}.`
      : `Katam ${u.latest} is available. You run ${u.current}.`;
    $("#update-notes").href = u.notes || "#";
    $("#update-notes").hidden = !u.notes;
    $("#btn-rollout").hidden = u.blocked;
    $("#update-banner").hidden = false;
  } catch { /* no feed configured or offline */ }
}
$("#btn-rollout").addEventListener("click", () => {
  if (!confirm("Upgrade every node, one at a time? Each node goes into maintenance (its VMs move away), upgrades, and comes back.")) return;
  const os = confirm("Also install Ubuntu security updates on the nodes? (OK = yes, Cancel = Katam packages only)");
  const reboot = os && confirm("Reboot nodes that need it after updating?");
  run("/api/v1/updates/rollout", "POST", { include_os: os, reboot });
});

/* ---------- tabs ---------- */
$$(".tabs button").forEach((b) => b.addEventListener("click", () => {
  state.tab = b.dataset.tab;
  $$(".tabs button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  $$("[data-panel]").forEach((p) => (p.hidden = p.dataset.panel !== state.tab));
  loadTab();
}));
$("#project-filter").addEventListener("change", (e) => { state.project = e.target.value; renderVms(); renderRack(); });

function loadTab() {
  const loaders = { protection: loadProtection, images: loadImages, security: loadSecurity, runbooks: loadRunbooks, dr: loadDr, reports: loadReports, access: loadAccess };
  if (loaders[state.tab]) loaders[state.tab]().catch((e) => toast(e.message));
}

function toast(msg) {
  const li = el("li", { class: "failed" }, el("time", {}, ""), el("span", { class: "kind" }, "Error"), el("span", { class: "msg" }, msg), el("span"));
  $("#task-list").prepend(li);
  setTimeout(() => li.remove(), 8000);
}

/* ---------- rack ---------- */
const scoped = () => state.vms.filter((v) => !state.project || v.project === state.project);

function renderRack() {
  $("#rack").replaceChildren(...state.nodes.map((n) => {
    const cls = `node ${n.ha_status === "maintenance" ? "maint" : ""} ${["fenced", "suspect"].includes(n.ha_status) ? n.ha_status : ""}`;
    if (!n.online) {
      return el("article", { class: `${cls} offline` }, el("div", { class: "ear" }),
        el("div", { class: "node-id" }, el("h3", {}, el("span", { class: "led" }), n.name), el("p", {}, n.host)),
        el("div", { class: "memmap" }),
        el("div", { class: "node-stats" }, el("span", {}, n.ha_status === "ok" ? "Unreachable" : n.ha_status),
          el("span", { class: "ha-note" }, n.ha_detail || "Check SSH and libvirtd"), nodeActions(n)),
        el("div", { class: "ear" }));
    }
    const vms = state.vms.filter((v) => v.node === n.name);
    const total = n.memory_mb;
    const segs = vms.map((v) => el("button", {
      class: `seg ${v.state}`, type: "button", style: `flex: 0 0 ${(v.memory_mb / total) * 100}%`,
      title: `${v.name} (${v.project}): ${gb(v.memory_mb)} GB, ${v.state}`, onclick: () => focusVm(v.name),
    }, v.name));
    const used = vms.reduce((a, v) => a + v.memory_mb, 0);
    const reserve = Math.min(n.reserve_mb, Math.max(0, total - used));
    segs.push(el("div", { class: "seg free" }));
    segs.push(el("div", { class: "seg reserve", style: `flex: 0 0 ${(reserve / total) * 100}%`, title: "Host and Ceph reserve" }));
    return el("article", { class: cls }, el("div", { class: "ear" }),
      el("div", { class: "node-id" }, el("h3", {}, el("span", { class: "led" }), n.name),
        el("p", {}, `${n.host}, ${n.threads} threads${n.site !== "primary" ? `, ${n.site} site` : ""}`)),
      el("div", { class: "memmap", role: "group", "aria-label": `${n.name} memory` }, segs),
      el("div", { class: "node-stats" },
        el("span", {}, el("strong", {}, `${gb(n.committed_mb)} / ${gb(total)} GB`), " committed"),
        el("span", {}, el("strong", {}, `${n.vcpus_allocated} / ${n.threads}`), " vCPU"),
        n.gpu_total ? el("span", {}, el("strong", {}, `${n.gpu_free} of ${n.gpu_total}`), " GPUs free") : null,
        n.ha_status !== "ok" ? el("span", { class: "ha-note" }, n.ha_detail || n.ha_status) : null,
        nodeActions(n)),
      el("div", { class: "ear" }));
  }));
  const online = state.nodes.filter((n) => n.online).length;
  const vms = scoped();
  $("#summary").textContent = `${online} of ${state.nodes.length} nodes online. ${vms.filter((v) => v.state === "running").length} of ${vms.length} VMs running.`;
}

function nodeActions(n) {
  if (!state.me.admin) return null;
  const out = [];
  if (n.ha_status === "ok") out.push(btn("Maintenance", () => confirm(`Move every VM off ${n.name} and put it in maintenance?`) && run(`/api/v1/nodes/${n.name}/maintenance`, "POST", { on: true })));
  if (n.ha_status === "maintenance") out.push(btn("Back in service", () => run(`/api/v1/nodes/${n.name}/maintenance`, "POST", { on: false })));
  if (n.ha_status === "suspect" && !n.online) out.push(btn("Confirm down", () => confirm(`Only confirm if ${n.name} is powered off. Its HA VMs will start on other nodes.`) && run(`/api/v1/nodes/${n.name}/confirm-down`, "POST", {}), "small danger"));
  if (n.ha_status === "fenced") out.push(btn("Recover", () => run(`/api/v1/nodes/${n.name}/recover`, "POST", {})));
  return out.length ? el("span", { class: "row-actions" }, out) : null;
}

function focusVm(name) {
  $("#filter").value = ""; state.filter = ""; renderVms();
  const row = document.querySelector(`tr[data-vm="${CSS.escape(name)}"]`);
  if (!row) return;
  row.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  row.classList.add("flash");
  setTimeout(() => row.classList.remove("flash"), 1600);
}

/* ---------- VM table ---------- */
function actionsFor(v) {
  const power = (a) => () => run(`/api/v1/vms/${v.name}/power/${a}`, "POST");
  const out = [];
  if (v.state === "stopped" || v.state === "crashed") out.push(btn("Start", power("start")));
  if (v.state === "running") {
    out.push(btn("Console", () => openConsole(v.name)), btn("Shut down", power("shutdown")), btn("Reboot", power("reboot")));
  }
  if (v.state === "paused") out.push(btn("Resume", power("resume")));
  if (v.state !== "stopped") out.push(btn("Power off", () => confirm(`Power off ${v.name}? Unsaved work in the guest is lost.`) && power("poweroff")(), "small danger"));
  if (!v.gpus) out.push(btn("Move", () => openMigrate(v)));
  if (v.state === "stopped") {
    out.push(btn("Export", () => exportVm(v)), btn("Make template", () => captureVm(v)));
    out.push(btn("Delete", () => confirm(`Delete ${v.name} and its disks? This can't be undone.`) && run(`/api/v1/vms/${v.name}`, "DELETE"), "small danger"));
  }
  out.push(btn("Snapshots", () => openSnapshots(v)));
  if (state.catalog.sdn) out.push(btn("Groups", () => editVmGroups(v)));
  return out;
}

function renderVms() {
  const f = state.filter.toLowerCase();
  const list = scoped().filter((v) => !f || [v.name, v.node, v.project, ...(v.ips || [])].some((s) => (s || "").toLowerCase().includes(f)));
  $("#vm-table tbody").replaceChildren(...list.map((v) => el("tr", { "data-vm": v.name },
    el("td", {}, el("strong", {}, v.name),
      v.gpus ? el("span", { class: "badge", title: v.gpu_model || "Passthrough GPU" }, `${v.gpus} GPU`) : null,
      v.ha ? null : el("span", { class: "badge", title: "Won't restart elsewhere if its node fails" }, "no HA")),
    el("td", {}, el("span", { class: `state ${v.state}` }, v.state)),
    el("td", {}, v.project), el("td", {}, v.node),
    el("td", { class: "num" }, String(v.vcpus)), el("td", { class: "num" }, `${gb(v.memory_mb)} GB`),
    el("td", {}, (v.ips || []).join(", ") || el("span", { class: "muted" }, v.state === "running" ? "Waiting for guest agent" : "")),
    el("td", {}, el("div", { class: "row-actions" }, actionsFor(v))))));
  $("#vm-empty").hidden = scoped().length > 0;
}
$("#filter").addEventListener("input", (e) => { state.filter = e.target.value; renderVms(); });

/* ---------- tasks and polling ---------- */
function renderTasks() {
  $("#task-list").replaceChildren(...state.tasks.slice(0, 12).map((t) => el("li", { class: t.status, title: t.log || "" },
    el("time", {}, new Date(t.created * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })),
    el("span", { class: "kind" }, `${t.kind} ${t.target}`), el("span", { class: "msg" }, t.message),
    el("span", { class: "muted" }, t.status === "running" ? "Working" : t.status === "done" ? "Done" : "Failed"))));
}

let busy = false;
async function refresh() {
  if (busy || $("#app").hidden) return;
  busy = true;
  try {
    const [nodes, vms, tasks] = await Promise.all([api("/api/v1/nodes"), api("/api/v1/vms"), api("/api/v1/tasks")]);
    const finished = state.tasks.some((t) => t.status === "running" && tasks.find((x) => x.id === t.id && x.status !== "running"));
    Object.assign(state, { nodes, vms, tasks });
    renderRack(); renderVms(); renderTasks();
    if (finished && state.tab !== "vms") loadTab();
  } catch (e) { $("#summary").textContent = e.message; } finally { busy = false; }
}
function schedule() {
  const fast = state.tasks.some((t) => t.status === "running");
  setTimeout(async () => { await refresh(); schedule(); }, fast ? 2000 : 6000);
}
async function run(path, method, body) {
  try {
    const r = await post(path, body, method);
    if (r && r.removed) toast(`Removed ${r.removed.length} stale definition(s)`);
    await refresh();
    return r;
  } catch (e) { toast(e.message); }
}

async function openConsole(name) {
  const w = window.open("about:blank", "_blank");
  try { const { url } = await post(`/api/v1/vms/${name}/console`); w.location = url; }
  catch (e) { w.close(); toast(e.message); }
}

function exportVm(v) {
  const fmt = prompt(`Export ${v.name} as: ${state.catalog.export_formats.join(", ")}\n(OVA for VMware, VHDX for Hyper-V, qcow2 for Proxmox)`, "ova");
  if (fmt) run(`/api/v1/vms/${v.name}/export`, "POST", { format: fmt.trim().toLowerCase() });
}
function captureVm(v) {
  const name = prompt(`Name for the new image and template made from ${v.name}`, `${v.name}-golden`);
  if (name) run(`/api/v1/vms/${v.name}/capture`, "POST", { name: name.trim().toLowerCase() });
}
function editVmGroups(v) {
  const groups = prompt(`Security groups for ${v.name}, separated by spaces.\nAvailable: ${state.catalog.secgroups.join(", ")}`, (v.secgroups || []).join(" ") || "default");
  if (groups !== null) run(`/api/v1/vms/${v.name}`, "PATCH", { secgroups: groups.split(/[\s,]+/).filter(Boolean) });
}

/* ---------- dialogs ---------- */
for (const d of $$("dialog")) {
  d.addEventListener("click", (e) => { if (e.target.matches("[data-close]")) d.close(); });
}
function showError(form, msg) { const p = $(".error", form); p.textContent = msg; p.hidden = !msg; }
const opts = (sel, items, blank) => sel.replaceChildren(...(blank ? [el("option", { value: "" }, blank)] : []),
  ...items.map((i) => (typeof i === "string" ? el("option", { value: i }, i) : el("option", { value: i.value }, i.label))));

async function openNew() {
  const c = state.catalog = await api("/api/v1/catalog");
  const f = $("#form-new");
  f.reset(); showError(f, "");
  opts(f.project, c.projects); f.project.value = state.project || c.projects[0] || "default";
  opts(f.template, c.templates.map((t) => ({ value: t.name, label: `${t.label} (${t.spec.vcpus} vCPU, ${t.spec.memory_gb} GB)` })));
  opts(f.image, c.images.map((i) => ({ value: i.name, label: i.label })));
  opts(f.iso, c.isos);
  opts(f.gpu_model, c.gpu_models, "Any available");
  opts(f.node, c.nodes, "Pick automatically");
  $("#row-gpu").hidden = !c.gpu_models.length;
  $("#net-choices").replaceChildren(...c.networks.map((n, i) => el("label", {}, el("input", { type: "checkbox", name: "networks", value: n.name, checked: i === 0 }), ` ${n.label}`)));
  $("#sg-field").hidden = !c.sdn;
  $("#sg-choices").replaceChildren(...c.secgroups.map((s) => el("label", {}, el("input", { type: "checkbox", name: "secgroups", value: s, checked: s === "default" }), ` ${s}`)));
  if (!c.templates.length) f.kind.value = "image";
  syncKind(); fillFromTemplate();
  $("#dlg-new").showModal();
}
function syncKind() {
  const f = $("#form-new");
  for (const e of $$("[data-kind]", f)) e.hidden = e.dataset.kind !== f.kind.value;
  $("#ci-fields").hidden = f.kind.value === "iso";
}
function fillFromTemplate() {
  const f = $("#form-new");
  const t = state.catalog.templates.find((x) => x.name === f.template.value);
  if (f.kind.value !== "template" || !t) return;
  for (const k of ["vcpus", "memory_gb", "disk_gb"]) if (t.spec[k]) f[k].value = t.spec[k];
  for (const cb of $$("[name=networks]", f)) cb.checked = (t.spec.networks || []).includes(cb.value);
}
$("#btn-new").addEventListener("click", () => openNew().catch((e) => toast(e.message)));
$("#form-new").addEventListener("change", (e) => { if (e.target.name === "kind") syncKind(); if (["kind", "template"].includes(e.target.name)) fillFromTemplate(); });
$("#form-new").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const kind = f.kind.value;
  const body = {
    name: f.vmname.value.trim(), project: f.project.value,
    template: kind === "template" ? f.template.value : undefined,
    image: kind === "image" ? f.image.value : undefined,
    iso: kind === "iso" ? f.iso.value : undefined,
    os_type: kind === "iso" ? f.os_type.value : undefined,
    vcpus: +f.vcpus.value, memory_gb: +f.memory_gb.value, disk_gb: +f.disk_gb.value,
    networks: $$("[name=networks]:checked", f).map((x) => x.value),
    secgroups: $$("[name=secgroups]:checked", f).map((x) => x.value),
    node: f.node.value || undefined, firmware: f.firmware.value || undefined,
    ip: kind !== "iso" ? f.ip.value.trim() || undefined : undefined,
    ssh_keys: kind !== "iso" ? f.ssh_keys.value : undefined,
    gpus: +f.gpus.value || 0, gpu_model: f.gpu_model.value || undefined,
    start: f.start.checked, autostart: f.autostart.checked, ha: f.ha.checked,
  };
  if (kind === "template" && !body.template) return showError(f, "Pick a template, or switch to Image or ISO");
  if (!body.networks.length) return showError(f, "Pick at least one network");
  try { await post("/api/v1/vms", body); $("#dlg-new").close(); refresh(); } catch (e) { showError(f, e.message); }
});

let migrating = null;
function openMigrate(v) {
  migrating = v;
  const f = $("#form-migrate");
  showError(f, "");
  $("#mig-name").textContent = v.name;
  opts(f.node, state.nodes.filter((n) => n.online && n.name !== v.node && n.site === "primary" && n.ha_status === "ok").map((n) => n.name));
  $("#dlg-migrate").showModal();
}
$("#form-migrate").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try { await post(`/api/v1/vms/${migrating.name}/migrate`, { node: ev.target.node.value }); $("#dlg-migrate").close(); refresh(); }
  catch (e) { showError(ev.target, e.message); }
});

/* generic editor dialog: JSON or YAML text */
let editor = null;
function openEditor({ title, help, name, nameEditable = true, body, save, extra }) {
  editor = { save, extra };
  const f = $("#form-edit");
  showError(f, "");
  $("#edit-title").textContent = title;
  $("#edit-help").textContent = help || "";
  $("#edit-name-row").hidden = name === undefined;
  f.name.value = name || ""; f.name.readOnly = !nameEditable;
  f.body.value = body;
  $("#edit-output").hidden = true;
  $("#edit-extra").hidden = !extra;
  if (extra) $("#edit-extra").textContent = extra.label;
  $("#edit-save").hidden = !save;
  $("#dlg-edit").showModal();
}
$("#form-edit").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  try { await editor.save(f.name.value.trim(), f.body.value); $("#dlg-edit").close(); loadTab(); refresh(); }
  catch (e) { showError(f, e.message); }
});
$("#edit-extra").addEventListener("click", async () => {
  const f = $("#form-edit");
  try {
    const out = await editor.extra.run(f.name.value.trim(), f.body.value);
    $("#edit-output").textContent = typeof out === "string" ? out : JSON.stringify(out, null, 2);
    $("#edit-output").hidden = false; showError(f, "");
  } catch (e) { showError(f, e.message); }
});
const parseJson = (t) => { try { return JSON.parse(t); } catch (e) { throw new Error(`Not valid JSON: ${e.message}`); } };

/* ---------- images, templates, exports ---------- */
async function loadImages() {
  const [imgs, tpls, exps] = await Promise.all([api("/api/v1/images"), api("/api/v1/templates"), api("/api/v1/exports").catch(() => [])]);
  $("#img-table tbody").replaceChildren(...imgs.map((i) => el("tr", {},
    el("td", {}, el("strong", {}, i.name), el("div", { class: "muted" }, i.label)),
    el("td", {}, el("span", { class: `state ${i.status === "ready" ? "running" : i.status === "failed" ? "crashed" : "paused"}` }, i.status)),
    el("td", {}, i.os_type), el("td", {}, i.firmware), el("td", { class: "num" }, i.size_gb ? `${i.size_gb} GB` : ""),
    el("td", {}, i.project || "Shared"), el("td", { class: "muted" }, (i.source || "").slice(0, 48)),
    el("td", {}, btn("Delete", () => confirm(`Delete image ${i.name}?`) && run(`/api/v1/images/${i.name}`, "DELETE").then(loadImages), "small danger")))));
  $("#tpl-table tbody").replaceChildren(...tpls.map((t) => el("tr", {},
    el("td", {}, el("strong", {}, t.name), el("div", { class: "muted" }, t.label)),
    el("td", {}, t.spec.image || t.spec.iso), el("td", {}, `${t.spec.vcpus} vCPU, ${t.spec.memory_gb} GB, ${t.spec.disk_gb} GB disk`),
    el("td", {}, (t.spec.networks || []).join(", ")), el("td", {}, t.project || "Shared"),
    el("td", {}, el("div", { class: "row-actions" },
      btn("Edit", () => editTemplate(t)),
      btn("Delete", () => confirm(`Delete template ${t.name}?`) && post(`/api/v1/templates/${t.name}`, undefined, "DELETE").then(loadImages).catch((e) => toast(e.message)), "small danger"))))));
  $("#exp-table tbody").replaceChildren(...exps.map((x) => el("tr", {},
    el("td", {}, el("a", { href: `/api/v1/exports/${encodeURIComponent(x.file)}` }, x.file)),
    el("td", { class: "num" }, size(x.size)), el("td", {}, when(x.created)))));
}
function editTemplate(t) {
  const spec = t ? t.spec : { image: (state.catalog.images[0] || {}).name || "", vcpus: 2, memory_gb: 4, disk_gb: 40, networks: [state.catalog.networks[0].name], ha: true };
  openEditor({
    title: t ? `Template ${t.name}` : "New template", name: t ? t.name : "", nameEditable: !t,
    help: "VM settings in JSON: image or iso, vcpus, memory_gb, disk_gb, networks, and optionally data_disks, secgroups, firmware, os_type, gpus. Add \"project\": \"...\" outside spec by editing via the API to scope it.",
    body: JSON.stringify(spec, null, 2),
    save: (name, text) => post(`/api/v1/templates/${name}`, { label: t ? t.label : name, project: t ? t.project : null, spec: parseJson(text) }, "PUT"),
  });
}
$("#btn-tpl").addEventListener("click", () => editTemplate(null));

$("#btn-import").addEventListener("click", () => {
  const f = $("#form-import");
  f.reset(); showError(f, "");
  opts(f.project, state.catalog.projects, "Every project");
  syncSrc();
  $("#dlg-import").showModal();
});
function syncSrc() { const f = $("#form-import"); for (const e of $$("[data-src]", f)) e.hidden = e.dataset.src !== f.src.value; }
$("#form-import").addEventListener("change", (e) => { if (e.target.name === "src") syncSrc(); });
$("#form-import").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const meta = { name: f.name.value.trim(), project: f.project.value, os_type: f.os_type.value, firmware: f.firmware.value,
    cloud_init: String(f.cloud_init.checked), v2v: String(f.v2v.checked) };
  try {
    if (f.src.value === "url") {
      await post("/api/v1/images", { ...meta, url: f.url.value.trim() });
    } else {
      if (!f.file.files[0]) return showError(f, "Choose a file");
      const fd = new FormData();
      Object.entries(meta).forEach(([k, v]) => fd.append(k, v));
      fd.append("file", f.file.files[0]);
      showError(f, "Uploading… keep this page open.");
      await api("/api/v1/images", { method: "POST", body: fd });
    }
    $("#dlg-import").close(); loadImages(); refresh();
  } catch (e) { showError(f, e.message); }
});

/* ---------- security groups ---------- */
async function loadSecurity() {
  const groups = await api("/api/v1/security-groups");
  $("#sg-note").hidden = !state.catalog.sdn;
  if (!state.catalog.sdn) {
    $("#sg-list").replaceChildren(el("p", { class: "empty" }, "Micro-segmentation needs SDN. Set sdn.enabled and use ovn-vlan or ovn-overlay networks in config.yaml."));
    return;
  }
  const users = (name) => state.vms.filter((v) => (v.secgroups || []).includes(name)).map((v) => v.name);
  $("#sg-list").replaceChildren(...groups.map((g) => el("article", { class: "card" },
    el("h3", {}, g.name, el("span", { class: "row-actions" }, btn("Edit", () => editGroup(g)),
      g.name !== "default" ? btn("Delete", () => confirm(`Delete group ${g.name}?`) && post(`/api/v1/security-groups/${g.name}`, undefined, "DELETE").then(loadSecurity).catch((e) => toast(e.message)), "small danger") : null)),
    el("p", { class: "muted" }, g.description || ""),
    el("ul", {}, g.rules.map((r) => el("li", {}, `${r.direction === "ingress" ? "Allow in" : "Allow out"} ${r.protocol}${r.ports ? ` ${r.ports}` : ""} ${r.direction === "ingress" ? "from" : "to"} ${r.remote_group ? `group ${r.remote_group}` : r.remote}`))),
    el("p", { class: "muted" }, `Members: ${users(g.name).join(", ") || "none"}`))));
}
function editGroup(g) {
  openEditor({
    title: g ? `Security group ${g.name}` : "New security group", name: g ? g.name : "", nameEditable: !g,
    help: "Rules in JSON. Each rule: direction (ingress or egress), protocol (any, tcp, udp, icmp), optional ports (22 or 8000-8100), and remote (a CIDR) or remote_group (another group). Everything else is denied and logged.",
    body: JSON.stringify(g ? g.rules : [{ direction: "ingress", protocol: "tcp", ports: "443", remote: "172.20.10.0/24" }, { direction: "egress", protocol: "any", remote: "0.0.0.0/0" }], null, 2),
    save: (name, text) => post(`/api/v1/security-groups/${name}`, { description: g ? g.description : "", rules: parseJson(text), project: g ? g.project : null }, "PUT"),
  });
}
$("#btn-sg").addEventListener("click", () => editGroup(null));

/* ---------- protection ---------- */
let snapVm = null;
async function openSnapshots(v) {
  snapVm = v;
  const f = $("#form-snap");
  f.reset(); showError(f, "");
  $("#snap-vm").textContent = v.name;
  await renderSnapshots();
  $("#dlg-snap").showModal();
}
async function renderSnapshots() {
  const { snapshots } = await api(`/api/v1/vms/${snapVm.name}/snapshots`);
  $("#snap-empty").hidden = snapshots.length > 0;
  $("#snap-table tbody").replaceChildren(...snapshots.map((s) => el("tr", {},
    el("td", {}, el("strong", {}, s.name), el("div", { class: "muted" }, s.kind === "scheduled" ? `schedule ${s.policy}` : s.created_by)),
    el("td", {}, when(s.created)),
    el("td", {}, s.locked ? when(s.locked_until) : el("span", { class: "muted" }, "Not locked")),
    el("td", {}, s.consistent ? "Yes" : "Crash-consistent"),
    el("td", {}, el("div", { class: "row-actions" },
      btn("Restore", () => {
        if (prompt(`Restoring discards everything written to ${snapVm.name} since this snapshot. Type the VM name to confirm.`) !== snapVm.name) return;
        run(`/api/v1/snapshots/${s.id}/restore`, "POST", { confirm: snapVm.name }).then(() => $("#dlg-snap").close());
      }),
      btn("To vault", () => { const d = prompt("Keep the vault copy immutable for how many days?", "30"); if (d) run(`/api/v1/snapshots/${s.id}/vault`, "POST", { days: +d }); }),
      s.locked ? null : btn("Delete", () => confirm(`Delete snapshot ${s.name}?`) && post(`/api/v1/snapshots/${s.id}`, undefined, "DELETE").then(renderSnapshots).catch((e) => showError($("#form-snap"), e.message)), "small danger"))))));
}
$("#form-snap").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  const vd = +f.vault_days.value || 0;
  try {
    await post(`/api/v1/vms/${snapVm.name}/snapshots`, { name: f.name.value.trim() || undefined, lock_days: +f.lock_days.value || 0, vault: vd > 0, vault_days: vd || undefined });
    f.reset(); await refresh(); setTimeout(() => renderSnapshots().catch(() => {}), 2500);
  } catch (e) { showError(f, e.message); }
});

async function loadProtection() {
  const [pols, vault, cov] = await Promise.all([api("/api/v1/protection/policies"), api("/api/v1/protection/vault"), api("/api/v1/protection/coverage")]);
  $("#pol-table tbody").replaceChildren(...pols.map((p) => el("tr", {},
    el("td", {}, el("strong", {}, p.name), p.enabled ? null : el("span", { class: "badge" }, "paused")),
    el("td", {}, p.project ? `project ${p.project}` : p.vms.join(", ")),
    el("td", {}, `${p.interval_h} h`), el("td", { class: "num" }, String(p.keep)),
    el("td", {}, p.lock_days ? `${p.lock_days} days` : "None"), el("td", {}, p.vault ? `${p.vault_days} days` : "No"),
    el("td", {}, when(p.last_run)),
    el("td", {}, state.me.admin ? el("div", { class: "row-actions" }, btn("Edit", () => editPolicy(p)),
      btn("Delete", () => confirm(`Delete schedule ${p.name}? Existing snapshots stay.`) && post(`/api/v1/protection/policies/${p.name}`, undefined, "DELETE").then(loadProtection), "small danger")) : null))));
  const st = vault.status;
  $("#vault-status").textContent = !st.enabled ? "The vault isn't configured. Set protection.vault in config.yaml (an S3 bucket with Object Lock enabled)."
    : st.object_lock ? `Bucket ${st.bucket}: Object Lock on. Copies are immutable until their retention date.` : `Bucket ${st.bucket}: ${st.error}`;
  $("#vault-table tbody").replaceChildren(...vault.copies.map((c) => el("tr", {},
    el("td", {}, el("strong", {}, c.vm)), el("td", { class: "muted" }, c.id), el("td", { class: "num" }, String(c.objects.length)),
    el("td", { class: "num" }, size(c.objects.reduce((a, o) => a + (o.size || 0), 0))), el("td", {}, when(c.retain_until)),
    el("td", {}, c.status),
    el("td", {}, state.me.admin && c.status === "ok" ? btn("Restore as image", () => { const n = prompt("Name for the restored image", `${c.vm}-restore`); if (n) run(`/api/v1/protection/vault/${c.id}/restore`, "POST", { image: n.trim().toLowerCase() }); }) : null))));
  $("#cov-table tbody").replaceChildren(...cov.map((r) => el("tr", {},
    el("td", {}, el("strong", {}, r.vm)), el("td", {}, r.project), el("td", {}, r.policies || el("span", { class: "muted" }, "None")),
    el("td", { class: "num" }, String(r.snapshots)), el("td", { class: "num" }, String(r.locked)),
    el("td", {}, r.last_snapshot_h == null ? el("span", { class: "muted" }, "Never") : `${r.last_snapshot_h} h ago`),
    el("td", {}, r.vault_until || el("span", { class: "muted" }, "No copy")))));
}
function editPolicy(p) {
  openEditor({
    title: p ? `Schedule ${p.name}` : "New snapshot schedule", name: p ? p.name : "nightly", nameEditable: !p,
    help: "JSON: project (all its VMs) or vms (a list), interval_h, keep (how many scheduled snapshots per VM), lock_days (0 = no lock), vault (true/false) and vault_days.",
    body: JSON.stringify(p ? { project: p.project, vms: p.vms, interval_h: p.interval_h, keep: p.keep, lock_days: p.lock_days, vault: p.vault, vault_days: p.vault_days, enabled: p.enabled }
      : { project: "default", interval_h: 24, keep: 7, lock_days: 7, vault: false, vault_days: 30 }, null, 2),
    save: (name, text) => post(`/api/v1/protection/policies/${name}`, parseJson(text), "PUT"),
  });
}
$("#btn-pol").addEventListener("click", () => editPolicy(null));

/* ---------- runbooks ---------- */
async function loadRunbooks() {
  const rbs = await api("/api/v1/runbooks");
  $("#rb-table tbody").replaceChildren(...rbs.map((r) => el("tr", {},
    el("td", {}, el("strong", {}, r.name)), el("td", {}, r.description || ""), el("td", {}, when(r.updated)),
    el("td", {}, el("div", { class: "row-actions" }, btn("Run", () => runRunbook(r)), state.me.admin ? btn("Edit", () => editRunbook(r)) : null)))));
}
async function editRunbook(r) {
  let body = r ? r.source : "";
  if (!r) { const ex = await api("/api/v1/runbooks/examples"); body = Object.values(ex)[0]; }
  openEditor({
    title: r ? `Runbook ${r.name}` : "New runbook", name: r ? r.name : "restart-with-health-check", nameEditable: !r,
    help: "YAML. Actions: vm.create, vm.power, vm.delete, vm.migrate, vm.export, wait.state, sleep, http.check, tcp.check, guest.exec, secgroup.apply, dr.run, runbook.call. Per step: retries, retry_delay, on_failure (stop, continue, rollback), rollback.",
    body, save: (name, text) => post(`/api/v1/runbooks/${name}`, { source: text }, "PUT"),
  });
}
function runRunbook(r) {
  openEditor({
    title: `Run ${r.name}`, help: "Parameters as JSON. Preview shows each step with your values filled in, without running anything.",
    body: "{\n  \"vm\": \"\"\n}", name: r.name, nameEditable: false,
    extra: { label: "Preview", run: (name, text) => post(`/api/v1/runbooks/${name}/run`, { params: parseJson(text), dry_run: true }).then((x) => x.plan) },
    save: (name, text) => post(`/api/v1/runbooks/${name}/run`, { params: parseJson(text) }),
  });
  $("#edit-save").textContent = "Run";
}
$("#btn-rb").addEventListener("click", () => editRunbook(null));
$("#dlg-edit").addEventListener("close", () => { $("#edit-save").textContent = "Save"; });

/* ---------- DR ---------- */
async function loadDr() {
  const plans = await api("/api/v1/dr/plans");
  $("#dr-table tbody").replaceChildren(...plans.map((p) => {
    const r = p.last_result || {};
    return el("tr", {},
      el("td", {}, el("strong", {}, p.name)), el("td", {}, p.spec.site || "dr"),
      el("td", {}, p.spec.vms.map((v) => v.name).join(", ")), el("td", {}, r.at ? `${when(r.at)} (${r.mode})` : "Never tested"),
      el("td", { class: "num" }, r.rto_s != null ? `${r.rto_s}s` : ""), el("td", { class: "num" }, r.rpo_s != null ? `${r.rpo_s}s` : ""),
      el("td", {}, el("div", { class: "row-actions" },
        btn("Test", () => run(`/api/v1/dr/plans/${p.name}/run`, "POST", { mode: "test" })),
        btn("Clean up test", () => run(`/api/v1/dr/plans/${p.name}/run`, "POST", { mode: "cleanup-test" })),
        state.me.admin ? btn("Fail over", () => failover(p), "small danger") : null)));
  }));
}
function failover(p) {
  const mode = prompt("Type planned (primary still up, no data loss) or disaster (primary is gone)", "planned");
  if (!mode) return;
  const confirmName = prompt(`This starts ${p.spec.vms.length} VM(s) at the DR site. Type the plan name to confirm.`);
  if (confirmName === p.name) run(`/api/v1/dr/plans/${p.name}/run`, "POST", { mode: mode.trim(), confirm: confirmName });
}
$("#btn-dr").addEventListener("click", () => openEditor({
  title: "New DR plan", name: "core-services",
  help: "JSON: site (from config dr.sites), vms in boot order with optional delay_s, network_map (primary bridge to DR bridge) and test_network_map (to an isolated network for tests).",
  body: JSON.stringify({ site: "dr", vms: [{ name: "db-01", order: 1, delay_s: 60 }, { name: "web-01", order: 2 }], network_map: { br20: "br20" }, test_network_map: { "*": "br-drtest" } }, null, 2),
  save: (name, text) => post(`/api/v1/dr/plans/${name}`, parseJson(text), "PUT"),
}));

/* ---------- costs and reports ---------- */
async function loadReports() {
  const [s, reps] = await Promise.all([api("/api/v1/finops/summary?days=30"), api("/api/v1/reports")]);
  const money = (x) => `${s.currency} ${Number(x).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  const total = s.projects.reduce((a, p) => a + p.total, 0);
  const savings = s.rightsizing.reduce((a, r) => a + (r.monthly_saving || 0), 0);
  $("#finops").replaceChildren(
    el("div", { class: "stat" }, el("span", { class: "muted" }, "Allocated cost"), el("strong", {}, money(total))),
    el("div", { class: "stat" }, el("span", { class: "muted" }, "Power (BMC metered)"), el("strong", {}, money(s.power_cost)), el("span", { class: "muted" }, `${s.power_kwh} kWh`)),
    el("div", { class: "stat" }, el("span", { class: "muted" }, "Possible monthly savings"), el("strong", {}, money(savings)), el("span", { class: "muted" }, `${s.rightsizing.length} recommendation(s)`)),
    el("div", { class: "stat wide" }, el("span", { class: "muted" }, "By project"),
      el("ul", {}, s.projects.map((p) => el("li", {}, `${p.project}: ${money(p.total)} (${p.vms} VMs, ${p.vcpu_hours} vCPU-hours)`)))),
    s.rightsizing.length ? el("div", { class: "stat wide" }, el("span", { class: "muted" }, "Rightsizing"),
      el("ul", {}, s.rightsizing.slice(0, 8).map((r) => el("li", {}, `${r.vm}: ${r.finding}. ${r.action}.`)))) : null);
  const days = () => $("#rep-days").value;
  $("#rep-table tbody").replaceChildren(...reps.map((r) => el("tr", {},
    el("td", {}, el("strong", {}, r.name)), el("td", {}, r.description),
    el("td", {}, el("div", { class: "row-actions" },
      el("a", { class: "button small", href: "#", onclick: (e) => { e.preventDefault(); window.open(`/api/v1/reports/${r.name}?format=html&days=${days()}`); } }, "View"),
      el("a", { class: "button small", href: "#", onclick: (e) => { e.preventDefault(); location.href = `/api/v1/reports/${r.name}?format=csv&days=${days()}`; } }, "CSV"))))));
}

/* ---------- access ---------- */
async function loadAccess() {
  const toks = await api("/api/v1/tokens");
  $("#tok-table tbody").replaceChildren(...toks.map((t) => el("tr", {},
    el("td", {}, t.name), el("td", {}, t.user_name), el("td", {}, when(t.last_used)), el("td", {}, t.expires ? when(t.expires) : "No expiry"),
    el("td", {}, btn("Revoke", () => confirm(`Revoke ${t.name}?`) && post(`/api/v1/tokens/${t.id}`, undefined, "DELETE").then(loadAccess), "small danger")))));
  if (!state.me.admin) return;
  const [users, b, projects] = await Promise.all([api("/api/v1/users"), api("/api/v1/role-bindings"), api("/api/v1/projects")]);
  $("#user-table tbody").replaceChildren(...users.map((u) => el("tr", {},
    el("td", {}, el("strong", {}, u.name), el("div", { class: "muted" }, u.display || "")), el("td", {}, u.source), el("td", {}, u.email || ""),
    el("td", {}, u.disabled ? "Disabled" : "Active"),
    el("td", {}, btn(u.disabled ? "Enable" : "Disable", () => post(`/api/v1/users/${u.name}`, { disabled: !u.disabled }, "PATCH").then(loadAccess).catch((e) => toast(e.message)))))));
  $("#bind-table tbody").replaceChildren(...b.bindings.map((x) => el("tr", {},
    el("td", {}, x.subject), el("td", {}, x.role), el("td", {}, x.project === "*" ? "All projects" : x.project),
    el("td", {}, btn("Remove", () => post(`/api/v1/role-bindings/${x.id}`, undefined, "DELETE").then(loadAccess), "small danger")))));
  $("#proj-table tbody").replaceChildren(...projects.map((p) => el("tr", {},
    el("td", {}, el("strong", {}, p.name), el("div", { class: "muted" }, p.description || "")),
    el("td", { class: "num" }, String(p.usage.vms || 0)), el("td", { class: "num" }, String(p.usage.vcpus || 0)),
    el("td", { class: "num" }, `${Math.round(p.usage.memory_gb || 0)} GB`),
    el("td", {}, Object.entries(p.quota).map(([k, v]) => `${k} ${v}`).join(", ") || "None"),
    el("td", {}, btn("Quota", () => openEditor({ title: `Quota for ${p.name}`, help: "JSON with any of: vms, vcpus, memory_gb, gpus. {} removes limits.",
      body: JSON.stringify(p.quota, null, 2), save: (_, text) => post(`/api/v1/projects/${p.name}`, { quota: parseJson(text) }, "PATCH") }))))));
}
$("#btn-user").addEventListener("click", () => openEditor({
  title: "Add a local user", name: "", help: "JSON with password (12+ characters), and optionally display and email. Directory and SSO users appear on their first sign-in.",
  body: JSON.stringify({ password: "", display: "", email: "" }, null, 2),
  save: (name, text) => post("/api/v1/users", { name, ...parseJson(text) }),
}));
$("#btn-bind").addEventListener("click", () => openEditor({
  title: "Grant a role", help: "Roles: admin, operator, viewer, auditor. Project * means every project.",
  body: JSON.stringify({ subject: "group:Katam-Operators", role: "operator", project: "default" }, null, 2),
  save: (_, text) => post("/api/v1/role-bindings", parseJson(text)),
}));
$("#btn-proj").addEventListener("click", () => openEditor({
  title: "New project", name: "", help: "JSON with description, cost_center and quota (vms, vcpus, memory_gb, gpus).",
  body: JSON.stringify({ description: "", cost_center: "", quota: { vcpus: 32, memory_gb: 128 } }, null, 2),
  save: (name, text) => post("/api/v1/projects", { name, ...parseJson(text) }),
}));
$("#btn-pass").addEventListener("click", async () => {
  if (state.me.source !== "local") return toast("Change this password in your directory or identity provider");
  const pw = prompt("New password (12 or more characters)");
  if (!pw) return;
  if (pw !== prompt("Type it again")) return toast("The two passwords didn't match");
  try { await post(`/api/v1/users/${state.me.user}`, { password: pw }, "PATCH"); toast("Password changed"); }
  catch (e) { toast(e.message); }
});
$("#btn-token").addEventListener("click", async () => {
  const name = prompt("Name this token (for example terraform or ci)", "terraform");
  if (!name) return;
  try {
    const r = await post("/api/v1/tokens", { name });
    openEditor({ title: "Your new token", help: "Copy it now. It won't be shown again.", body: r.token });
    loadAccess();
  } catch (e) { toast(e.message); }
});

start().catch(() => showLogin());
