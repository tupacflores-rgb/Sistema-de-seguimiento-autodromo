/* Página de administrador MultiCam Vision: 4 secciones independientes.
 * Sin dependencias externas (funciona sin internet, solo en la red local).
 */
(function () {
  "use strict";

  // Este servidor solo entiende http (no tiene certificado). Si el navegador entró en
  // https (autocompletado de la barra de direcciones, "HTTPS-First" de Chrome, un marcador
  // viejo, etc.) ninguna llamada a la API funciona y antes se perdía todo lo que se
  // estaba editando sin aviso. Se corrige solo, antes de que se ejecute el resto de la página.
  if (location.protocol === "https:") {
    location.replace("http://" + location.host + location.pathname + location.search + location.hash);
    return;
  }

  // ================================================================ utilidades
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function el(tag, attrs = {}, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) if (c != null) e.append(c instanceof Node ? c : document.createTextNode(String(c)));
    return e;
  }

  async function api(method, url, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    let resp;
    try { resp = await fetch(url, opts); } catch (e) {
      // Se muestra el motivo real (p. ej. "Failed to fetch", un error de certificado, etc.)
      // en vez de un mensaje genérico, para poder diagnosticar problemas de red reales.
      const reason = (e && e.message) ? e.message : "error de red";
      const err = new Error(`No hay conexión con el servidor de administración (${reason})`);
      err.data = {}; throw err;
    }
    let data = {};
    try { data = await resp.json(); } catch (_) { /* respuesta sin JSON */ }
    if (!resp.ok || data.ok === false) {
      const err = new Error(data.error || `Error HTTP ${resp.status}`);
      err.data = data; err.status = resp.status; throw err;
    }
    return data;
  }

  let toastTimer;
  function toast(msg, isError = false) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.toggle("error", isError);
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), isError ? 5000 : 2600);
  }

  function setMsg(node, text, kind = "") { node.textContent = text || ""; node.className = "msg" + (kind ? " " + kind : ""); }

  const STATE_LABEL = {
    streaming: ["Transmitiendo", "ok"], connecting: ["Conectando…", "warn"], backoff: ["Reintentando", "error"],
    idle: ["En espera", "off"], disabled: ["Deshabilitada", "off"], stopped: ["Detenida", "off"],
  };
  function stateChip(node, state) {
    const [label, cls] = STATE_LABEL[state] || [state || "—", "off"];
    node.className = "chip " + cls;
    node.textContent = label;
  }

  function fmt(v, suffix = "") { return v == null ? "—" : `${v}${suffix}`; }

  // ================================================================ unidad de medida
  // Todo se guarda y se calcula SIEMPRE en milímetros; esto solo cambia cómo se muestran
  // los números (y lo que se escribe en los campos) para quien prefiera trabajar en cm.
  const U = (() => {
    let unit = "mm";
    try { if (localStorage.getItem("mcv.unit") === "cm") unit = "cm"; } catch (_) { /* privado/bloqueado */ }
    const factor = () => (unit === "cm" ? 0.1 : 1);
    return {
      get unit() { return unit; },
      set(u) {
        unit = u === "cm" ? "cm" : "mm";
        try { localStorage.setItem("mcv.unit", unit); } catch (_) { /* privado/bloqueado */ }
      },
      factor,
      label() { return unit; },
      // mm (interno) -> número a mostrar en un <input>
      toInput(mm, decimals = unit === "cm" ? 1 : 0) {
        if (mm == null) return "";
        const v = mm * factor();
        return decimals ? Math.round(v * 10 ** decimals) / 10 ** decimals : Math.round(v);
      },
      // número de un <input> (en la unidad elegida) -> mm (interno)
      fromInput(v) { return Number(v) / factor(); },
      // texto con unidad para mostrar (no para inputs)
      fmt(mm, decimals = unit === "cm" ? 1 : 0) {
        if (mm == null) return "—";
        return `${this.toInput(mm, decimals)} ${unit}`;
      },
      // para pasarle a TrackEditor y que convierta lo que dibuja en el canvas
      view() { return { factor: factor(), label: unit }; },
    };
  })();

  // ================================================================ estado global
  const S = {
    cameras: [],
    section: null,
    leaveHooks: {},
  };

  async function loadCameras() {
    const d = await api("GET", "/api/cameras");
    S.cameras = d.cameras;
    const w = $("#cameraWarning");
    w.hidden = !d.warning; w.textContent = d.warning || "";
    return S.cameras;
  }
  const usable = () => S.cameras.filter((c) => c.enabled && c.host);

  // ================================================================ navegación
  const SECTIONS = { cameras: enterCameras, live: enterLive, config: enterConfig, track: enterTrack,
                     vehicles: enterVehicles, tracking: enterTracking, esp32: enterEsp32 };
  const HASH = { cameras: "#camaras", live: "#video", config: "#configuracion", track: "#pista",
                 vehicles: "#vehiculos", tracking: "#tracking", esp32: "#esp32" };

  function showSection(name, push = true) {
    if (!SECTIONS[name]) name = "cameras";
    if (S.section && S.section !== name && S.leaveHooks[S.section]) S.leaveHooks[S.section]();
    S.section = name;
    for (const sec of $$(".section")) sec.hidden = sec.id !== "sec-" + name;
    for (const t of $$(".tab")) t.setAttribute("aria-selected", t.dataset.section === name ? "true" : "false");
    if (push && location.hash !== HASH[name]) history.replaceState(null, "", HASH[name]);
    SECTIONS[name]().then(() => updateSetupProgress()).catch((e) => { toast(e.message, true); updateSetupProgress(); });
  }
  for (const t of $$(".tab")) t.addEventListener("click", () => showSection(t.dataset.section));
  window.addEventListener("hashchange", () => {
    const name = Object.keys(HASH).find((k) => HASH[k] === location.hash);
    if (name && name !== S.section) showSection(name, false);
  });

  // ================================================================ estado en la barra superior
  async function refreshGlobal() {
    const box = $("#globalStatus");
    const chips = [];
    for (const c of S.cameras) {
      let state = c.enabled ? (c.host ? "idle" : "disabled") : "disabled";
      if (c.enabled && c.host) {
        try { state = (await api("GET", `/api/cameras/${c.id}/state`)).state; } catch (_) { state = "backoff"; }
      }
      const chip = el("span", { title: `${c.name}: ${(STATE_LABEL[state] || [state])[0]}` });
      stateChip(chip, state);
      chip.textContent = c.name;
      chips.push(chip);
    }
    box.replaceChildren(...chips);
    try {
      const t = await api("GET", "/api/tracking/state");
      $("#btnGoTracking").classList.toggle("on", !!t.running);
    } catch (_) { /* sin estado */ }
  }
  setInterval(() => { if (!document.hidden) refreshGlobal().catch(() => {}); }, 3000);

  // ================================================================ 1. CÁMARAS
  async function enterCameras() {
    await loadCameras();
    renderCameraCards();
  }

  function renderCameraCards() {
    const box = $("#cameraCards");
    box.replaceChildren(...S.cameras.map(cameraCard));
    if (!S.cameras.length) box.append(el("div", { class: "empty", text: "No hay cámaras. Agregá una con el botón de arriba." }));
  }

  function cameraCard(cam) {
    const form = $("#tplCameraCard").content.firstElementChild.cloneNode(true);
    form.dataset.id = cam.id;
    $(".cam-name", form).textContent = cam.name;
    $(".cam-id", form).textContent = cam.id;
    form.classList.toggle("disabled", !cam.enabled);
    const f = form.elements;
    f.enabled.checked = cam.enabled;
    f.name.value = cam.name;
    f.host.value = cam.host;
    f.port.value = cam.port;
    f.video_path.value = cam.video_path;
    f.username.value = cam.username || "";
    f.password.value = "";
    f.password.placeholder = cam.has_password ? "•••• (guardada)" : "";
    f.notes.value = cam.notes || "";
    $(".card-url", form).textContent = cam.video_url ? `Video: ${cam.video_url}` : "Sin dirección configurada";

    f.enabled.addEventListener("change", () => form.classList.toggle("disabled", !f.enabled.checked));

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      clearFieldErrors(form);
      const body = {
        name: f.name.value, enabled: f.enabled.checked, host: f.host.value.trim(),
        port: f.port.value, video_path: f.video_path.value.trim(), username: f.username.value,
        notes: f.notes.value, processing: cam.processing,
      };
      if (f.password.value) body.password = f.password.value;
      const btn = $("button[type=submit]", form);
      btn.disabled = true;
      try {
        const d = await api("PUT", `/api/cameras/${cam.id}`, body);
        const i = S.cameras.findIndex((c) => c.id === cam.id);
        S.cameras[i] = d.camera;
        form.replaceWith(cameraCard(d.camera));
        toast(`${d.camera.name}: guardada`);
        refreshGlobal().catch(() => {});
      } catch (err) {
        showFieldErrors(form, err.data && err.data.fields);
        toast(err.message, true);
      } finally { btn.disabled = false; }
    });

    $("[data-act=test]", form).addEventListener("click", async (e) => {
      const out = $(".test-result", form);
      const btn = e.currentTarget;
      btn.disabled = true;
      out.className = "test-result small";
      out.textContent = "Probando… (guardá antes si cambiaste la dirección)";
      try {
        const r = await api("POST", `/api/cameras/${cam.id}/test`);
        const lines = [];
        const c = r.control || {};
        lines.push(c.ok ? `Control  OK · ${c.response_ms} ms · resolución ${c.video_size} · calidad ${c.quality} · zoom ${c.zoom}`
                        : `Control  FALLA · ${c.error}`);
        const v = r.video || {};
        lines.push(v.ok ? `Video    OK · ${v.width}×${v.height} · primer frame en ${v.first_frame_ms} ms`
                        : `Video    FALLA · ${v.error}`);
        out.textContent = lines.join("\n");
        out.classList.add(r.ok ? "ok" : "error");
      } catch (err) {
        out.textContent = err.message;
        out.classList.add("error");
      } finally { btn.disabled = false; refreshGlobal().catch(() => {}); }
    });

    $("[data-act=delete]", form).addEventListener("click", async () => {
      if (!confirm(`¿Eliminar la cámara «${cam.name}»? También se borran sus dibujos de pista.`)) return;
      try {
        await api("DELETE", `/api/cameras/${cam.id}`);
        S.cameras = S.cameras.filter((c) => c.id !== cam.id);
        renderCameraCards();
        toast("Cámara eliminada");
        refreshGlobal().catch(() => {});
      } catch (err) { toast(err.message, true); }
    });
    return form;
  }

  function clearFieldErrors(form) {
    $$(".field-error", form).forEach((n) => n.remove());
    $$(".invalid", form).forEach((n) => n.classList.remove("invalid"));
  }
  function showFieldErrors(form, fields) {
    if (!fields) return;
    for (const [k, msg] of Object.entries(fields)) {
      const name = k.split(".").pop();
      const input = form.elements[name] || form.elements[k];
      if (input && input.closest) {
        input.classList.add("invalid");
        input.closest("label").append(el("span", { class: "field-error", text: msg }));
      }
    }
  }

  $("#btnAddCamera").addEventListener("click", async () => {
    const ids = new Set(S.cameras.map((c) => c.id));
    let n = 1; while (ids.has(`cam${n}`)) n++;
    try {
      const d = await api("POST", "/api/cameras", { id: `cam${n}`, name: `Cámara ${n}`, enabled: false, host: "", port: 8080, video_path: "/video" });
      S.cameras.push(d.camera);
      renderCameraCards();
      const card = $(`form[data-id="cam${n}"]`);
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.elements.host.focus();
      toast(`Se agregó cam${n}: completá la IP y habilitala`);
    } catch (err) { toast(err.message, true); }
  });

  // ================================================================ 2. VIDEO EN VIVO
  let liveTimer = null;

  async function enterLive() {
    await loadCameras();
    const grid = $("#liveGrid");
    stopLive();
    const cams = usable();
    if (!cams.length) {
      grid.replaceChildren(el("div", { class: "empty", text: "No hay cámaras habilitadas con IP. Configuralas en la sección 1." }));
      return;
    }
    grid.replaceChildren(...cams.map(liveTile));
    const poll = async () => {
      for (const c of cams) {
        const tile = $(`[data-live="${c.id}"]`);
        if (!tile) continue;
        try { updateMetrics(tile, await api("GET", `/api/cameras/${c.id}/state`)); } catch (_) { /* sigue */ }
      }
    };
    poll();
    liveTimer = setInterval(poll, 1000);
  }

  function streamUrl(cid, fps) { return `/api/cameras/${cid}/stream.mjpg?fps=${fps || 0}&t=${Date.now()}`; }

  function attachStream(img, cid, fps, overlay) {
    img.onload = () => { if (overlay) overlay.hidden = true; };
    img.onerror = () => {
      if (overlay) { overlay.hidden = false; overlay.textContent = "Sin video. Reintentando en 3 s…"; }
      clearTimeout(img._retry);
      img._retry = setTimeout(() => { if (img.isConnected && img.dataset.on === "1") img.src = streamUrl(cid, fps); }, 3000);
    };
    img.dataset.on = "1";
    img.src = streamUrl(cid, fps);
  }

  function detachStream(img) {
    if (!img) return;
    img.dataset.on = "0";
    clearTimeout(img._retry);
    img.onerror = null; img.onload = null;
    img.removeAttribute("src"); // cierra la conexión del stream
  }

  function liveTile(cam) {
    const fps = Number($("#liveFps").value);
    const chip = el("span", { class: "chip off", text: "—" });
    const img = el("img", { alt: `Video de ${cam.name}` });
    const overlay = el("div", { class: "overlay-msg", text: "Conectando…" });
    const tile = el("div", { class: "panel live-tile", "data-live": cam.id },
      el("div", { class: "panel-title" }, el("span", { text: cam.name }), el("code", { class: "muted small", text: `${cam.host}:${cam.port}` }), el("span", { class: "spacer" }), chip),
      el("div", { class: "video-box" }, img, overlay),
      el("div", { class: "metrics" },
        metric("FPS recibidos", "fps"), metric("Resolución", "res"), metric("Edad último frame", "age"),
        metric("KB / frame", "kb"), metric("Reconexiones", "rec"), metric("Conectada hace", "since"),
        el("div", { class: "metric wide", hidden: true, "data-m": "errbox" }, el("div", { class: "k", text: "Último error" }), el("div", { class: "v err", "data-v": "err" }))));
    tile._chip = chip;
    attachStream(img, cam.id, fps, overlay);
    return tile;
  }

  function metric(label, key) {
    return el("div", { class: "metric" }, el("div", { class: "k", text: label }), el("div", { class: "v", "data-v": key, text: "—" }));
  }

  function updateMetrics(box, st) {
    const set = (k, v) => { const n = $(`[data-v="${k}"]`, box); if (n) n.textContent = v; };
    set("fps", fmt(st.fps_received));
    set("res", st.width ? `${st.width}×${st.height}` : "—");
    set("age", fmt(st.last_frame_age_ms, " ms"));
    set("kb", fmt(st.last_frame_kb));
    set("rec", fmt(st.reconnects));
    set("since", st.connected_for_s != null ? `${st.connected_for_s} s` : "—");
    const eb = $('[data-m="errbox"]', box);
    if (eb) { eb.hidden = !st.last_error; set("err", st.last_error || ""); }
    const chip = box._chip || $(".chip", box);
    if (chip) stateChip(chip, st.state);
  }

  function stopLive() {
    clearInterval(liveTimer); liveTimer = null;
    $$("#liveGrid img").forEach(detachStream);
  }
  S.leaveHooks.live = () => { stopLive(); $("#liveGrid").replaceChildren(); };
  $("#liveFps").addEventListener("change", () => { if (S.section === "live") enterLive(); });

  // ================================================================ 3. CONFIGURACIÓN
  const C = { cam: null, timer: null, status: null };

  async function enterConfig() {
    await loadCameras();
    const cams = usable();
    $("#configEmpty").hidden = cams.length > 0;
    $("#configLayout").hidden = cams.length === 0;
    const pills = $("#configCamPills");
    pills.replaceChildren(...cams.map((c) => el("button", {
      class: "pill", "aria-pressed": "false", "data-cam": c.id, text: c.name,
      onclick: () => selectConfigCam(c.id),
    })));
    if (!cams.length) return;
    const keep = cams.find((c) => c.id === C.cam) ? C.cam : cams[0].id;
    await selectConfigCam(keep);
  }

  function stopConfig() {
    clearInterval(C.timer); C.timer = null;
    detachStream($("#configPreview"));
  }
  S.leaveHooks.config = stopConfig;

  async function selectConfigCam(cid) {
    stopConfig();
    C.cam = cid;
    const cam = S.cameras.find((c) => c.id === cid);
    $$("#configCamPills .pill").forEach((p) => p.setAttribute("aria-pressed", p.dataset.cam === cid ? "true" : "false"));
    $("#configGeomBanner").hidden = true;
    setMsg($("#quickMsg"), ""); setMsg($("#procMsg"), "");
    // vista previa a 10 fps para no saturar
    attachStream($("#configPreview"), cid, 10, null);
    const mbox = $("#configMetrics");
    if (!mbox.children.length) {
      mbox.append(metric("FPS recibidos", "fps"), metric("Resolución", "res"), metric("Edad último frame", "age"), metric("KB / frame", "kb"),
        el("div", { class: "metric wide", hidden: true, "data-m": "errbox" }, el("div", { class: "k", text: "Último error" }), el("div", { class: "v err", "data-v": "err" })));
    }
    mbox._chip = $("#configState");
    const poll = async () => { try { updateMetrics(mbox, await api("GET", `/api/cameras/${cid}/state`)); } catch (_) { /* sigue */ } };
    poll();
    C.timer = setInterval(poll, 1000);
    // parámetros del sistema
    const pf = $("#procForm").elements;
    for (const k of ["max_frame_age_ms", "latency_offset_ms", "target_fps"]) pf[k].value = cam.processing[k];
    await loadIpwStatus();
  }

  async function loadIpwStatus() {
    const cid = C.cam;
    const tbody = $("#ipwTable tbody");
    const errBox = $("#ipwError");
    errBox.hidden = true;
    tbody.replaceChildren(el("tr", {}, el("td", { colspan: 3, class: "muted", text: "Leyendo ajustes del teléfono…" })));
    try {
      const d = await api("GET", `/api/cameras/${cid}/ipwebcam/status`);
      if (cid !== C.cam) return;
      C.status = d.status;
      renderIpwTable(d.status, new Set(d.geometry_keys));
      $("#ipwResponse").textContent = `respuesta ${d.status._response_ms} ms`;
    } catch (err) {
      tbody.replaceChildren();
      errBox.hidden = false;
      errBox.textContent = `No se pudieron leer los ajustes: ${err.message}`;
      $("#quickZoom").replaceChildren();
    }
  }

  const HIDDEN_KEYS = new Set(["torch", "zoom"]);

  function renderIpwTable(st, geo) {
    const cur = st.curvals || {}, avail = st.avail || {};
    // zoom (por índice)
    const zsel = $("#quickZoom");
    const zooms = avail.zoom || [];
    zsel.replaceChildren(...zooms.map((z, i) => el("option", { value: i, text: `${z}${/^\d+$/.test(z) ? " %" : ""}` })));
    const zi = zooms.indexOf(String(cur.zoom));
    if (zi >= 0) zsel.value = String(zi);
    zsel.disabled = !zooms.length;

    const keys = Object.keys(cur).filter((k) => !HIDDEN_KEYS.has(k)).sort();
    const rows = keys.map((key) => {
      const value = String(cur[key]);
      let input;
      if (Array.isArray(avail[key]) && avail[key].length) {
        input = el("select", {}, ...avail[key].map((o) => el("option", { value: o, text: o })));
        if (!avail[key].includes(value)) input.prepend(el("option", { value, text: value }));
        input.value = value;
      } else {
        input = el("input", { value, type: /^-?\d+(\.\d+)?$/.test(value) ? "number" : "text" });
      }
      const btn = el("button", { class: "btn small", type: "button", text: "Aplicar", disabled: true });
      const tr = el("tr", {}, el("td", {}, key, geo.has(key) ? el("span", { class: "geo", title: "Cambia la geometría de la imagen", text: "⚠" }) : null),
        el("td", {}, input), el("td", {}, btn));
      const onEdit = () => { const ch = String(input.value) !== value; btn.disabled = !ch; tr.classList.toggle("changed", ch); };
      input.addEventListener("input", onEdit); input.addEventListener("change", onEdit);
      btn.addEventListener("click", () => ipwAction({ action: "setting", key, value: input.value }, btn));
      return tr;
    });
    $("#ipwTable tbody").replaceChildren(...rows);
    if (!rows.length) $("#ipwTable tbody").append(el("tr", {}, el("td", { colspan: 3, class: "muted", text: "El teléfono no informó ajustes." })));
  }

  async function ipwAction(body, btn) {
    const msg = $("#quickMsg");
    if (btn) btn.disabled = true;
    try {
      const d = await api("POST", `/api/cameras/${C.cam}/ipwebcam/action`, body);
      setMsg(msg, d.message && d.message !== "Ok" ? d.message : "Aplicado", "ok");
      if (d.affects_calibration) $("#configGeomBanner").hidden = false;
      toast("Ajuste aplicado");
      setTimeout(loadIpwStatus, 700);
    } catch (err) {
      setMsg(msg, err.message, "error");
      toast(err.message, true);
      if (btn) btn.disabled = false;
    }
  }

  for (const b of $$("[data-quick]")) {
    b.addEventListener("click", () => {
      const q = b.dataset.quick;
      const body = q === "recommended" ? { action: "recommended" }
        : q === "torch-on" ? { action: "torch", value: true }
        : q === "torch-off" ? { action: "torch", value: false }
        : { action: "focus", value: true };
      ipwAction(body, b).finally(() => { b.disabled = false; });
    });
  }
  $("#quickZoom").addEventListener("change", (e) => ipwAction({ action: "zoom", value: Number(e.target.value) }));
  $("#btnIpwRefresh").addEventListener("click", loadIpwStatus);

  $("#procForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target.elements;
    const processing = {};
    for (const k of ["max_frame_age_ms", "latency_offset_ms", "target_fps"]) processing[k] = Number(f[k].value);
    try {
      const d = await api("PUT", `/api/cameras/${C.cam}/processing`, { processing });
      const i = S.cameras.findIndex((c) => c.id === C.cam);
      S.cameras[i] = d.camera;
      setMsg($("#procMsg"), "Guardado", "ok");
    } catch (err) {
      const fields = (err.data && err.data.fields) || {};
      setMsg($("#procMsg"), Object.values(fields).join(" · ") || err.message, "error");
    }
  });

  // ================================================================ 4. PISTA
  const T = { track: null, viewKey: "plan", dirty: false, editor: null, images: {}, undo: new Map(),
              mode: "draw", linked: null, calibActive: -1, hs: {}, bg: "" };
  const HG = TrackEditor.Homography;
  const STATIC_EMPTY = Object.freeze({ centerline: { points: [], closed: true }, border_outer: { points: [], closed: true },
                                       inner_islands: [], exclusion_zones: [] });

  function emptyLayers() {
    return {
      centerline: { points: [], closed: true },
      border_outer: { points: [], closed: true },
      inner_islands: [],
      exclusion_zones: [],
    };
  }

  // -- borrador local: red de seguridad ante cualquier corte (red, recarga, bug) mientras se
  // edita la pista. No reemplaza al guardado: es solo para poder recuperar lo que se estaba
  // haciendo si algo interrumpe antes de llegar a "Guardar pista".
  const TRACK_DRAFT_KEY = "mcv.track.draft";
  let draftTimer = null;
  function saveDraft() {
    try { localStorage.setItem(TRACK_DRAFT_KEY, JSON.stringify({ at: Date.now(), track: T.track })); } catch (_) { /* ignorado */ }
  }
  function clearDraft() { try { localStorage.removeItem(TRACK_DRAFT_KEY); } catch (_) { /* ignorado */ } }
  function readDraft() {
    let raw; try { raw = localStorage.getItem(TRACK_DRAFT_KEY); } catch (_) { return null; }
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (_) { clearDraft(); return null; }
  }

  function setDirty(v) {
    T.dirty = v;
    setMsg($("#trackMsg"), v ? "Cambios sin guardar" : "", v ? "error" : "");
    clearTimeout(draftTimer);
    if (v) draftTimer = setTimeout(saveDraft, 800);
  }

  function maybeOfferDraftRecovery() {
    if (T.draftChecked) return;
    T.draftChecked = true;
    const draft = readDraft();
    if (!draft || !draft.track) return;
    if (JSON.stringify(draft.track) === JSON.stringify(T.track)) { clearDraft(); return; }
    const when = new Date(draft.at).toLocaleTimeString();
    const box = el("div", { class: "banner warn" },
      `Hay cambios de la pista sin guardar de las ${when} en esta PC (por ejemplo, de un cierre inesperado del navegador). `,
      el("button", { class: "btn small", type: "button", text: "Recuperar borrador", onclick: () => {
        T.track = draft.track; setDirty(true); renderTrackViews(); selectTrackView(T.viewKey); box.remove();
        toast("Borrador recuperado: revisá y guardá si está bien");
      } }),
      " ",
      el("button", { class: "btn small ghost", type: "button", text: "Descartar", onclick: () => { clearDraft(); box.remove(); } }));
    $("#sec-track .section-head").after(box);
  }

  let sizePresets = [];       // predefinidas (chapadur típico)
  let userPresets = [];       // guardadas por el propio usuario
  async function loadSizePresets(force) {
    if (sizePresets.length && !force) return;
    try {
      const [built, own] = await Promise.all([
        sizePresets.length ? Promise.resolve({ presets: sizePresets }) : api("GET", "/api/track/size_presets"),
        api("GET", "/api/track/presets"),
      ]);
      sizePresets = built.presets || [];
      userPresets = own.presets || [];
    } catch (_) { return; }
    renderSizePresetOptions();
  }
  function renderSizePresetOptions() {
    const sel = $("#trackSizePreset");
    const prev = sel.value;
    const groups = [el("option", { value: "", text: "Elegir…" })];
    if (userPresets.length) {
      groups.push(el("optgroup", { label: "Mis plantillas" },
        ...userPresets.map((p) => el("option", { value: "u:" + p.id, text: `${p.name} (${p.width_mm}×${p.height_mm} mm)` }))));
    }
    groups.push(el("optgroup", { label: "Predefinidas" },
      ...sizePresets.map((p) => el("option", { value: "b:" + p.id, text: p.label }))));
    sel.replaceChildren(...groups);
    sel.value = prev && Array.from(sel.options).some((o) => o.value === prev) ? prev : "";
    $("#btnDeletePreset").hidden = !sel.value.startsWith("u:");
  }
  $("#trackSizePreset").addEventListener("change", (e) => {
    const v = e.target.value;
    $("#btnDeletePreset").hidden = !v.startsWith("u:");
    if (!v) return;
    const [kind, id] = [v.slice(0, 1), v.slice(2)];
    const p = kind === "u" ? userPresets.find((x) => x.id === id) : sizePresets.find((x) => x.id === id);
    if (!p) return;
    T.track.width_mm = p.width_mm; T.track.height_mm = p.height_mm;
    setDirty(true);
    selectTrackView(T.viewKey);
    if (T.viewKey === "linked") refreshLinkedViews();
    toast(`Medidas aplicadas: ${p.label || p.name}`);
  });
  $("#btnSavePreset").addEventListener("click", async () => {
    const name = prompt("Nombre para esta plantilla (por ejemplo, «Mi pista de 8»):", "");
    if (name === null) return;
    if (!name.trim()) { toast("Hace falta un nombre", true); return; }
    try {
      await api("POST", "/api/track/presets", { name: name.trim(), width_mm: T.track.width_mm, height_mm: T.track.height_mm });
      await loadSizePresets(true);
      toast("Plantilla guardada");
    } catch (e) { toast(e.message, true); }
  });
  $("#btnDeletePreset").addEventListener("click", async () => {
    const v = $("#trackSizePreset").value;
    if (!v.startsWith("u:")) return;
    const id = v.slice(2);
    const p = userPresets.find((x) => x.id === id);
    if (!confirm(`¿Borrar la plantilla «${p ? p.name : id}»?`)) return;
    try {
      await api("DELETE", "/api/track/presets/" + encodeURIComponent(id));
      $("#trackSizePreset").value = "";
      $("#btnDeletePreset").hidden = true;
      await loadSizePresets(true);
      toast("Plantilla borrada");
    } catch (e) { toast(e.message, true); }
  });

  async function enterTrack() {
    await loadCameras();
    loadSizePresets();
    if (!T.editor) {
      T.editor = new TrackEditor($("#trackCanvas"), {
        undoStacks: T.undo,
        onChange: () => { setDirty(true); renderLayerList(); },
        onCursor: (txt) => { $("#cursorPos").textContent = txt || "—"; },
        onInfo: renderLayerList,
        onMessage: (m) => toast(m),
      });
    }
    if (!T.track || !T.dirty) T.track = (await api("GET", "/api/track")).track;
    T.track.calibration = T.track.calibration || { points: [], homographies: {} };
    renderTrackViews();
    selectTrackView(T.viewKey);
    maybeOfferDraftRecovery();
  }

  $("#trackBg").addEventListener("change", (e) => {
    T.bg = e.target.value;
    if (T.viewKey === "plan") T.editor.updateViewProps({ bgImage: bgImage() });
    else if (T.viewKey === "linked") refreshLinkedViews();
  });

  function renderTrackViews() {
    renderBgSelect();
    const views = [{ key: "plan", label: "Plano (mm)" }, { key: "linked", label: "Vinculada (cámaras + plano)" },
      ...S.cameras.map((c) => ({ key: c.id, label: `Solo ${c.name} (px)` }))];
    if (!views.find((v) => v.key === T.viewKey)) T.viewKey = "plan";
    $("#trackViews").replaceChildren(...views.map((v) => el("button", {
      class: "pill", "data-view": v.key, "aria-pressed": v.key === T.viewKey ? "true" : "false", text: v.label,
      onclick: () => selectTrackView(v.key),
    })));
  }

  function camEntry(cid) { return T.track.cameras[cid]; }

  // ---- fondo del plano: foto enderezada de una cámara calibrada (calibración GUARDADA)
  function savedHom(cid) { return ((T.track.calibration || {}).homographies || {})[cid] || {}; }
  function renderBgSelect() {
    const sel = $("#trackBg");
    const opts = [el("option", { value: "", text: "Ninguno" })];
    for (const c of S.cameras) {
      const ok = !!savedHom(c.id).H_img_to_plan;
      opts.push(el("option", { value: c.id, text: `Foto enderezada · ${c.name}${ok ? "" : " (sin calibrar)"}`, disabled: !ok }));
    }
    sel.replaceChildren(...opts);
    if (T.bg && !savedHom(T.bg).H_img_to_plan) T.bg = "";
    sel.value = T.bg;
  }
  function bgImage() {
    if (!T.bg) return null;
    const stamp = savedHom(T.bg).computed_at || "";
    const key = `rect:${T.bg}@${stamp}`;
    if (!T.images[key]) {
      const img = new Image();
      img.onload = () => renderAll();
      img.onerror = () => toast("No se pudo generar la foto enderezada (¿falta OpenCV o guardar la calibración?)", true);
      img.src = `/api/track/cameras/${T.bg}/rectified.jpg?px_per_mm=0.5&t=${encodeURIComponent(stamp)}`;
      T.images[key] = img;
    }
    return T.images[key];
  }

  function selectTrackView(key) {
    T.viewKey = key;
    renderBgSelect();
    $$("#trackViews .pill").forEach((p) => p.setAttribute("aria-pressed", p.dataset.view === key ? "true" : "false"));
    const isPlan = key === "plan", isLinked = key === "linked";
    $("#planSettings").hidden = !(isPlan || isLinked);
    $("#camSettings").hidden = isPlan || isLinked;
    $("#linkedSettings").hidden = !isLinked;
    $("#singlePanel").hidden = isLinked;
    $("#linkedGrid").hidden = !isLinked;
    $("#linkedHint").hidden = true;
    if (isLinked) {
      $("#trackW").value = U.toInput(T.track.width_mm);
      $("#trackH").value = U.toInput(T.track.height_mm);
      $("#trackGrid").value = U.toInput(T.track.grid_mm);
      buildLinked();
      renderLayerList();
      return;
    }
    T.editor.setMode("draw");
    if (isPlan) {
      $("#trackW").value = U.toInput(T.track.width_mm);
      $("#trackH").value = U.toInput(T.track.height_mm);
      $("#trackGrid").value = U.toInput(T.track.grid_mm);
      T.editor.setView({
        key: "plan", undoKey: "plan", kind: "plan", width: T.track.width_mm, height: T.track.height_mm, bgImage: bgImage(),
        gridMm: T.track.grid_mm, snap: $("#trackSnap").checked, image: null, layers: T.track.plan, unit: U.view(),
      });
    } else {
      const cam = S.cameras.find((c) => c.id === key);
      const entry = camEntry(key);
      $("#btnSnapshot").disabled = !(cam && cam.enabled && cam.host);
      setMsg($("#snapMsg"), cam && cam.enabled && cam.host ? "" : "Habilitá la cámara y cargá su IP para capturar.");
      if (entry) {
        $("#snapInfo").textContent = `Imagen ${entry.image_width}×${entry.image_height} px · capturada ${entry.snapshot_taken_at || "—"}`;
        T.editor.setView({
          key, kind: "camera", width: entry.image_width, height: entry.image_height,
          image: loadSnapshotImage(key, entry.snapshot_taken_at), layers: entry.layers, readonly: false,
        });
      } else {
        $("#snapInfo").textContent = "Todavía no hay imagen de referencia para esta cámara. Las coordenadas se guardan en píxeles de esa imagen.";
        T.editor.setView({ key, kind: "camera", width: 960, height: 720, image: null, layers: emptyLayers(), readonly: true });
      }
    }
    T.editor.setActive(T.editor.active.layer === "zone" ? "centerline" : T.editor.active.layer);
    renderLayerList();
  }

  // ---------------------------------------------------------------- vista vinculada
  function curEd() { return T.viewKey === "linked" && T.linked ? T.linked.planEd : T.editor; }
  function allEds() { return T.viewKey === "linked" && T.linked ? [T.linked.planEd, ...T.linked.cams.map((c) => c.ed)] : [T.editor]; }
  function renderAll() { allEds().forEach((e) => e.render()); }
  function setActiveAll(layer, zone = -1) { allEds().forEach((e) => { e.active = { layer, zone }; e.render(); }); renderLayerList(); }

  function camsWithSnapshot() { return S.cameras.filter((c) => T.track.cameras[c.id]); }

  function computeHs() {
    T.hs = {};
    for (const cam of camsWithSnapshot()) {
      const pairs = T.track.calibration.points.filter((p) => p.plan && p.px && p.px[cam.id]);
      const r = { n: pairs.length };
      if (pairs.length >= 4) {
        try {
          const H = HG.fit(pairs.map((p) => p.px[cam.id]), pairs.map((p) => p.plan));
          const res = pairs.map((p) => { const q = HG.apply(H, ...p.px[cam.id]); return q ? Math.hypot(q[0] - p.plan[0], q[1] - p.plan[1]) : Infinity; });
          r.H = H; r.Hinv = HG.inv(H);
          r.rms = Math.sqrt(res.reduce((a, v) => a + v * v, 0) / res.length);
          r.max = Math.max(...res);
        } catch (e) { r.error = e.message; }
      } else r.error = `Faltan ${4 - pairs.length} punto(s)`;
      T.hs[cam.id] = r;
    }
  }

  function buildLinked() {
    const grid = $("#linkedGrid");
    const cams = camsWithSnapshot();
    const hint = $("#linkedHint");
    const missing = S.cameras.filter((c) => !T.track.cameras[c.id] && c.enabled && c.host);
    if (missing.length) {
      hint.hidden = false;
      hint.textContent = `Sin imagen de referencia: ${missing.map((c) => c.name).join(", ")}. Capturala en la vista «Solo ${missing[0].name} (px)».`;
    }
    const mk = (title, cls) => {
      const canvas = el("canvas", { tabindex: 0, "aria-label": title });
      const chip = el("span", { class: "chip off", text: "" });
      const status = el("span", { text: "—" });
      const panel = el("div", { class: `panel lk-panel ${cls || ""}` },
        el("div", { class: "lk-head" }, el("span", { text: title }), el("span", { class: "spacer" }), chip),
        canvas, el("div", { class: "canvas-status" }, status));
      return { panel, canvas, chip, status };
    };
    const opts = (statusEl) => ({
      undoStacks: T.undo,
      onChange: () => { setDirty(true); renderAll(); renderLayerList(); },
      onCursor: (txt) => { statusEl.textContent = txt || "—"; },
      onInfo: renderLayerList,
      onMessage: (m) => toast(m),
      onCalibClick: calibClick,
    });
    const camParts = cams.map((cam) => { const p = mk(`${cam.name} · foto`); return { cam, ...p }; });
    const planPart = mk("Plano (mm)", "plan");
    grid.replaceChildren(...camParts.map((p) => p.panel), planPart.panel);
    const active = (T.linked && T.linked.planEd.active) || (T.editor && T.editor.active) || { layer: "centerline", zone: -1 };
    T.linked = {
      planEd: new TrackEditor(planPart.canvas, opts(planPart.status)),
      planChip: planPart.chip,
      cams: camParts.map((p) => ({ cid: p.cam.id, cam: p.cam, chip: p.chip, ed: new TrackEditor(p.canvas, opts(p.status)) })),
    };
    planPart.chip.textContent = `${U.fmt(T.track.width_mm)} × ${U.fmt(T.track.height_mm)}`;
    allEds().forEach((e) => { e.active = { ...active }; });
    refreshLinkedViews();
    setMode(T.mode);
  }

  function refreshLinkedViews() {
    if (!T.linked) return;
    computeHs();
    const W = T.track.width_mm, Hh = T.track.height_mm;
    T.linked.planEd.setView({
      key: "L:plan", undoKey: "plan", kind: "plan", width: W, height: Hh, gridMm: T.track.grid_mm, bgImage: bgImage(),
      snap: $("#trackSnap").checked, image: null, layers: T.track.plan, overlay: calibOverlay("plan"), unit: U.view(),
    });
    for (const c of T.linked.cams) {
      const entry = T.track.cameras[c.cid];
      const h = T.hs[c.cid] || {};
      const ok = !!h.H;
      c.chip.className = "chip " + (ok ? (h.rms < 10 ? "ok" : "warn") : "error");
      c.chip.textContent = ok ? `calibrada · ${U.fmt(h.rms, 1)}` : `sin calibrar · ${h.n || 0}/4`;
      c.ed.setView({
        key: "L:" + c.cid, undoKey: "plan", kind: "camera", width: entry.image_width, height: entry.image_height,
        image: loadSnapshotImage(c.cid, entry.snapshot_taken_at), gridMm: T.track.grid_mm, snap: $("#trackSnap").checked,
        layers: ok ? T.track.plan : STATIC_EMPTY, worldBounds: { w: W, h: Hh }, unit: U.view(),
        xf: ok ? { fwd: (x, y) => HG.apply(h.Hinv, x, y), inv: (u, v) => HG.apply(h.H, u, v) } : null,
        readonly: !ok, readonlyMsg: "Cámara sin calibrar: cargá 4 puntos de referencia (modo «Puntos de referencia»)",
        banner: ok ? null : "Sin calibrar: marcá 4+ puntos de referencia para dibujar aquí",
        overlay: calibOverlay(c.cid),
      });
    }
    renderCalibList();
    renderCalibQuality();
  }

  function setMode(mode) {
    T.mode = mode;
    $$("#linkedSettings .seg-btn").forEach((b) => b.setAttribute("aria-pressed", b.dataset.mode === mode ? "true" : "false"));
    $("#calibPanel").hidden = mode !== "calib";
    if (T.linked) allEds().forEach((e) => e.setMode(mode));
    if (mode === "calib" && T.calibActive < 0 && T.track.calibration.points.length) T.calibActive = 0;
    renderCalibList();
  }
  $$("#linkedSettings .seg-btn").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

  function calibOverlay(target) {
    return (ctx, ed) => {
      if (T.viewKey !== "linked") return;
      const showAll = T.mode === "calib";
      const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#ff6b2c";
      T.track.calibration.points.forEach((p, i) => {
        const active = i === T.calibActive && showAll;
        if (!showAll && !active) { /* en modo dibujo se muestran tenues */ }
        let pos = null, proj = null;
        if (target === "plan") pos = p.plan;
        else {
          pos = p.px && p.px[target];
          const h = T.hs[target];
          if (h && h.Hinv && p.plan) proj = HG.apply(h.Hinv, p.plan[0], p.plan[1]);
        }
        ctx.globalAlpha = showAll ? 1 : 0.45;
        if (proj && pos) {
          const a = ed.toScreen(...pos), b = ed.toScreen(...proj);
          ctx.strokeStyle = "#ff6b6b"; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(...a); ctx.lineTo(...b); ctx.stroke();
          ctx.beginPath(); ctx.arc(b[0], b[1], 4, 0, Math.PI * 2); ctx.stroke();
        }
        if (!pos) return;
        const [sx, sy] = ed.toScreen(pos[0], pos[1]);
        const col = active ? accent : "#ffffff";
        ctx.strokeStyle = "rgba(0,0,0,.8)"; ctx.lineWidth = 4;
        ctx.beginPath(); ctx.moveTo(sx - 9, sy); ctx.lineTo(sx + 9, sy); ctx.moveTo(sx, sy - 9); ctx.lineTo(sx, sy + 9); ctx.stroke();
        ctx.strokeStyle = col; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(sx - 9, sy); ctx.lineTo(sx + 9, sy); ctx.moveTo(sx, sy - 9); ctx.lineTo(sx, sy + 9); ctx.stroke();
        ctx.beginPath(); ctx.arc(sx, sy, active ? 7 : 5, 0, Math.PI * 2); ctx.stroke();
        ctx.font = "600 11px system-ui, sans-serif";
        const w = ctx.measureText(p.name).width;
        ctx.fillStyle = "rgba(0,0,0,.7)"; ctx.fillRect(sx + 10, sy - 22, w + 8, 16);
        ctx.fillStyle = col; ctx.textBaseline = "middle"; ctx.fillText(p.name, sx + 14, sy - 14);
      });
      ctx.globalAlpha = 1;
    };
  }

  function calibClick(view, pt) {
    const pts = T.track.calibration.points;
    if (T.calibActive < 0 || !pts[T.calibActive]) { toast("Creá o elegí un punto de referencia primero"); return; }
    const p = pts[T.calibActive];
    if (view.key === "L:plan") {
      if (pt[0] < 0 || pt[1] < 0 || pt[0] > T.track.width_mm || pt[1] > T.track.height_mm) { toast("Fuera de la plancha"); return; }
      p.plan = [Math.round(pt[0]), Math.round(pt[1])];
    } else {
      const cid = view.key.slice(2);
      if (pt[0] < 0 || pt[1] < 0 || pt[0] > view.width || pt[1] > view.height) { toast("Fuera de la imagen"); return; }
      p.px = p.px || {};
      p.px[cid] = [Math.round(pt[0] * 10) / 10, Math.round(pt[1] * 10) / 10];
    }
    setDirty(true);
    refreshLinkedViews();
  }

  function renderCalibList() {
    const box = $("#calibList");
    if (!box || !T.track) return;
    const pts = T.track.calibration.points;
    const cams = camsWithSnapshot();
    box.replaceChildren(...pts.map((p, i) => {
      const name = el("input", { value: p.name, maxlength: 30, "aria-label": "Nombre del punto" });
      name.addEventListener("change", () => { p.name = name.value || `P${i + 1}`; setDirty(true); renderAll(); });
      const num = (idx) => {
        const inp = el("input", { type: "number", step: U.unit === "cm" ? 0.1 : 1, placeholder: idx ? `Y ${U.unit}` : `X ${U.unit}`,
          value: p.plan ? U.toInput(p.plan[idx]) : "" });
        inp.addEventListener("change", () => {
          const v = inp.value === "" ? null : U.fromInput(inp.value);
          if (v === null) { p.plan = null; } else { p.plan = p.plan || [0, 0]; p.plan[idx] = v; }
          setDirty(true); refreshLinkedViews();
        });
        return inp;
      };
      const row = el("div", { class: "calib-row", "aria-current": i === T.calibActive ? "true" : "false" },
        el("div", { class: "r1" }, name,
          el("button", { class: "btn small " + (i === T.calibActive ? "" : "ghost"), type: "button", text: i === T.calibActive ? "Ubicando" : "Ubicar",
            onclick: () => { T.calibActive = i; if (T.mode !== "calib") setMode("calib"); renderCalibList(); renderAll(); } }),
          el("button", { class: "btn small ghost danger", type: "button", text: "✕", title: "Eliminar punto",
            onclick: () => { pts.splice(i, 1); T.calibActive = Math.min(T.calibActive, pts.length - 1); setDirty(true); refreshLinkedViews(); } })),
        el("div", { class: "r2" }, num(0), num(1)),
        el("div", { class: "r3" },
          el("span", { class: "mini " + (p.plan ? "ok" : ""), text: p.plan ? "plano ✓" : "plano —" }),
          ...cams.map((c) => el("span", { class: "mini " + (p.px && p.px[c.id] ? "ok" : ""), text: `${c.name} ${p.px && p.px[c.id] ? "✓" : "—"}` }))));
      return row;
    }));
    if (!pts.length) box.append(el("div", { class: "muted small", text: "Todavía no hay puntos." }));
  }

  function renderCalibQuality() {
    const box = $("#calibQuality");
    if (!box) return;
    box.replaceChildren(...camsWithSnapshot().map((c) => {
      const h = T.hs[c.id] || {};
      const ok = !!h.H;
      const cam = savedHom(c.id).camera;
      return el("div", { class: "q " + (ok ? "ok" : "bad") },
        el("b", { text: c.name + ": " }),
        ok ? `${h.n} puntos · error medio ${U.fmt(h.rms, 1)} · máx ${U.fmt(h.max, 1)}` : (h.error || "sin datos"),
        cam ? el("div", { class: "muted small", text: `Cámara estimada a ${(cam.height_mm / 1000).toFixed(2)} m de altura, sobre (${U.fmt(cam.x_mm)}, ${U.fmt(cam.y_mm)}) del plano · campo de visión ${cam.hfov_deg}° (${cam.focal_method}). Compará con la medida real: si no se parece, revisá los puntos.` }) : null);
    }));
    if (T.track.calibration.points.length >= 4)
      box.append(el("div", { class: "muted small", text: "Con 4 puntos exactos el error siempre da ~0: usá 5 o más para que el número sirva como control." }));
  }

  $("#btnAddCalib").addEventListener("click", () => {
    const pts = T.track.calibration.points;
    pts.push({ name: `P${pts.length + 1}`, plan: null, px: {} });
    T.calibActive = pts.length - 1;
    setDirty(true);
    if (T.mode !== "calib") setMode("calib");
    refreshLinkedViews();
  });

  // Atajo para simplificar el seteo inicial: la mayoría de las veces las marcas de referencia
  // son las 4 esquinas de la plancha (fáciles de medir), así que se crean las 4 de una sola vez
  // con su posición en el plano ya completa; solo falta marcarlas en cada foto.
  $("#btnAdd4Corners").addEventListener("click", () => {
    const pts = T.track.calibration.points;
    const W = T.track.width_mm, Hh = T.track.height_mm;
    const before = pts.length;
    for (const [name, plan] of [["Esquina NO", [0, 0]], ["Esquina NE", [W, 0]], ["Esquina SE", [W, Hh]], ["Esquina SO", [0, Hh]]]) {
      pts.push({ name, plan, px: {} });
    }
    T.calibActive = before;
    setDirty(true);
    if (T.mode !== "calib") setMode("calib");
    refreshLinkedViews();
    toast("Se agregaron las 4 esquinas con su posición en el plano: marcalas ahora en cada foto");
  });

  function loadSnapshotImage(cid, stamp) {
    const cacheKey = `${cid}@${stamp}`;
    if (T.images[cacheKey]) return T.images[cacheKey];
    const img = new Image();
    img.onload = () => T.editor.render();
    img.onerror = () => toast("No se pudo cargar la imagen de referencia", true);
    img.src = `/api/track/cameras/${cid}/snapshot.jpg?t=${encodeURIComponent(stamp || Date.now())}`;
    T.images[cacheKey] = img;
    return img;
  }

  const LAYER_META = {
    centerline: { label: "Línea central", color: "--cv-center" },
    border_outer: { label: "Borde exterior", color: "--cv-outer" },
  };

  function renderLayerList() {
    const ed = curEd();
    if (!ed || !ed.view) return;
    const L = ed.view.layers;
    const st = TrackEditor.stats(L, ed.view.kind);
    const css = getComputedStyle(document.documentElement);
    const rows = [];
    for (const k of TrackEditor.LINE_LAYERS) {
      const active = ed.active.layer === k;
      const s = st[k];
      rows.push(el("button", {
        class: "layer", type: "button", "aria-pressed": active ? "true" : "false",
        onclick: () => setActiveAll(k),
      }, el("span", { class: "sw", style: `background:${css.getPropertyValue(LAYER_META[k].color)}` }),
        el("span", { text: LAYER_META[k].label }),
        el("span", { class: "meta", text: `${s.n} pts · ${Math.round(s.length)} ${s.unit}` })));
      if (active) {
        const cb = el("input", { type: "checkbox" });
        cb.checked = L[k].closed;
        cb.addEventListener("change", () => { ed.pushUndo(); L[k].closed = cb.checked; renderAll(); setDirty(true); renderLayerList(); });
        rows.push(el("label", { class: "layer-opts" }, cb, "Cerrada (circuito)"));
      }
    }
    $("#layerList").replaceChildren(...rows);

    const polySection = (kind, title, prefix, colorVar, emptyText) => {
      const key = TrackEditor.POLY[kind];
      const list = L[key] || [];
      const rows = list.map((z, i) => {
        const active = ed.active.layer === kind && ed.active.zone === i;
        const name = el("input", { value: z.name, maxlength: 40, "aria-label": `Nombre (${title})` });
        name.addEventListener("focus", () => { if (!(ed.active.layer === kind && ed.active.zone === i)) setActiveAll(kind, i); });
        name.addEventListener("change", () => { z.name = name.value || `${prefix} ${i + 1}`; renderAll(); setDirty(true); });
        const area = st[kind === "zone" ? "zones" : "islands"][i];
        const areaTxt = ed.view.kind === "plan" ? `${(area.area / 1e6).toFixed(3)} m²` : `${Math.round(area.area)} px²`;
        return el("div", { class: "zone-row", "aria-current": active ? "true" : "false" },
          name,
          el("button", { class: "btn small " + (active ? "" : "ghost"), type: "button", title: `${area.n} puntos · ${areaTxt}`, text: active ? "Editando" : "Editar",
            onclick: () => setActiveAll(kind, i) }),
          el("button", { class: "btn small ghost danger", type: "button", text: "✕", title: "Eliminar",
            onclick: () => { ed.removePoly(kind, i); setActiveAll(ed.active.layer, ed.active.zone); } }));
      });
      return [
        el("div", { class: "panel-title", style: "margin:6px 0 2px" },
          el("span", { class: "sw", style: `display:inline-block;width:14px;height:10px;border-radius:2px;background:${css.getPropertyValue(colorVar)}` }),
          title, el("span", { class: "spacer" }),
          el("button", { class: "btn small ghost", type: "button", text: `+ ${prefix === "Isla" ? "Nueva isla" : "Nueva zona"}`,
            onclick: () => { ed.addPoly(kind, `${prefix} ${list.length + 1}`); setActiveAll(ed.active.layer, ed.active.zone); } })),
        ...(rows.length ? rows : [el("div", { class: "muted small", text: emptyText })]),
      ];
    };
    $("#zoneList").replaceChildren(
      ...polySection("island", "Islas interiores", "Isla", "--cv-inner",
        "El césped de adentro de cada curva cerrada. Un circuito en 8 tiene DOS islas (una por cada lazo)."),
      ...polySection("zone", "Zonas a ignorar", "Zona", "--cv-zone",
        "Sin zonas. Usalas para reflejos, bordes de la mesa u objetos que no son autos."));

    let act;
    if (TrackEditor.POLY[ed.active.layer]) {
      const z = (L[TrackEditor.POLY[ed.active.layer]] || [])[ed.active.zone];
      act = `${ed.active.layer === "zone" ? "Zona" : "Isla"}: ${(z || {}).name || "—"}`;
    } else act = (LAYER_META[ed.active.layer] || { label: "—" }).label;
    $("#layerInfo").textContent = `Capa activa: ${act}${ed.view.readonly ? " · solo lectura (falta imagen)" : ""}`;
  }

  function numInput(id, prop, viewProp) {
    $(id).addEventListener("change", (e) => {
      const shown = Number(e.target.value);
      if (!(shown > 0)) return;
      const v = U.fromInput(shown);
      T.track[prop] = v;
      setDirty(true);
      if (T.viewKey === "linked") { refreshLinkedViews(); if (viewProp !== "gridMm") allEds().forEach((e) => e.fit()); }
      if (T.viewKey === "plan") {
        T.editor.updateViewProps({ [viewProp]: v });
        if (viewProp !== "gridMm") T.editor.fit();
        const outside = TrackEditor.LINE_LAYERS.some((k) => T.track.plan[k].points.some((p) => p[0] > T.track.width_mm || p[1] > T.track.height_mm))
          || [...T.track.plan.exclusion_zones, ...(T.track.plan.inner_islands || [])].some((z) => z.points.some((p) => p[0] > T.track.width_mm || p[1] > T.track.height_mm));
        if (outside) toast("Hay puntos fuera de las nuevas medidas: movelos antes de guardar", true);
      }
    });
  }
  numInput("#trackW", "width_mm", "width");
  numInput("#trackH", "height_mm", "height");
  numInput("#trackGrid", "grid_mm", "gridMm");
  $("#trackSnap").addEventListener("change", (e) => allEds().forEach((ed) => ed.view && ed.view.kind !== undefined && ed.updateViewProps({ snap: e.target.checked })));

  $("#btnUndo").addEventListener("click", () => { if (!curEd().undo()) toast("Nada para deshacer"); else renderAll(); });
  $("#btnClearLayer").addEventListener("click", () => {
    if (confirm("¿Borrar todos los puntos de la capa activa?")) { curEd().clearActive(); renderAll(); }
  });
  $("#btnFit").addEventListener("click", () => allEds().forEach((e) => e.fit()));
  function zoomCentered(ed, f) {
    if (!ed.view) return;
    const cx = ed.canvas.clientWidth / 2, cy = ed.canvas.clientHeight / 2;
    const [wx, wy] = ed.toWorld(cx, cy);
    ed.scale = Math.min(Math.max(ed.scale * f, 1e-4), 200);
    ed.ox = cx - wx * ed.scale; ed.oy = cy - wy * ed.scale;
    ed.render();
  }
  $("#btnZoomIn").addEventListener("click", () => zoomCentered(curEd(), 1.3));
  $("#btnZoomOut").addEventListener("click", () => zoomCentered(curEd(), 1 / 1.3));

  async function saveTrack() {
    const btn = $("#btnTrackSave");
    btn.disabled = true;
    try {
      const d = await api("PUT", "/api/track", T.track);
      T.track = d.track;
      setDirty(false);
      clearDraft();
      setMsg($("#trackMsg"), "Guardado", "ok");
      toast("Pista guardada");
      selectTrackView(T.viewKey); // re-vincula el editor con los datos guardados
    } catch (err) {
      const fields = (err.data && err.data.fields) || {};
      const detail = Object.entries(fields).map(([k, v]) => `${k}: ${v}`).join(" · ");
      setMsg($("#trackMsg"), detail || err.message, "error");
      toast(detail || err.message, true);
    } finally { btn.disabled = false; }
  }
  $("#btnTrackSave").addEventListener("click", saveTrack);

  $("#btnSnapshot").addEventListener("click", async () => {
    const cid = T.viewKey;
    const btn = $("#btnSnapshot");
    btn.disabled = true;
    setMsg($("#snapMsg"), "Capturando…");
    try {
      const d = await api("POST", `/api/track/cameras/${cid}/snapshot`);
      const entry = T.track.cameras[cid] || { layers: emptyLayers() };
      Object.assign(entry, { image_width: d.image_width, image_height: d.image_height, snapshot_taken_at: d.snapshot_taken_at });
      T.track.cameras[cid] = entry;
      setMsg($("#snapMsg"), d.size_changed
        ? "Atención: cambió la resolución. Los dibujos anteriores pueden quedar desalineados."
        : "Imagen actualizada", d.size_changed ? "error" : "ok");
      T.editor.undoStacks.delete(cid);
      T.editor.view = null; // fuerza re-encuadre
      selectTrackView(cid);
    } catch (err) {
      setMsg($("#snapMsg"), err.message, "error");
    } finally { btn.disabled = false; }
  });

  window.addEventListener("keydown", (e) => {
    if (S.section !== "track") return;
    const typing = /^(INPUT|SELECT|TEXTAREA)$/.test((document.activeElement || {}).tagName || "");
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveTrack(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !typing) { e.preventDefault(); curEd().undo(); renderAll(); }
  });
  window.addEventListener("beforeunload", (e) => { if (T.dirty) { e.preventDefault(); e.returnValue = ""; } });

  // ================================================================ 5. VEHÍCULOS
  const V = { cfg: null, dirty: false, previews: {}, pickFor: -1 };
  const PRESETS = [["Rojo", "#ff0000"], ["Verde", "#00ff00"], ["Azul", "#0000ff"], ["Magenta", "#ff00ff"],
                   ["Amarillo", "#ffff00"], ["Cian", "#00ffff"]];

  function hexToHsvCv(hex) {
    const r = parseInt(hex.slice(1, 3), 16) / 255, g = parseInt(hex.slice(3, 5), 16) / 255, b = parseInt(hex.slice(5, 7), 16) / 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    let h = 0;
    if (d) h = mx === r ? ((g - b) / d) % 6 : mx === g ? (b - r) / d + 2 : (r - g) / d + 4;
    h = (h * 60 + 360) % 360;
    return [Math.round(h / 2) % 180, Math.round(mx ? (d / mx) * 255 : 0), Math.round(mx * 255)];
  }
  function hsvCvToHex(h, s, v) {
    h = (h * 2) / 60; s /= 255; v /= 255;
    const c = v * s, x = c * (1 - Math.abs((h % 2) - 1)), m = v - c;
    const [r, g, b] = h < 1 ? [c, x, 0] : h < 2 ? [x, c, 0] : h < 3 ? [0, c, x] : h < 4 ? [0, x, c] : h < 5 ? [x, 0, c] : [c, 0, x];
    return "#" + [r, g, b].map((u) => Math.round((u + m) * 255).toString(16).padStart(2, "0")).join("");
  }
  function hueDist(a, b) { const d = Math.abs(hexToHsvCv(a)[0] - hexToHsvCv(b)[0]) * 2 % 360; return Math.min(d, 360 - d); }

  function setVehDirty(v) { V.dirty = v; setMsg($("#vehMsg"), v ? "Cambios sin guardar" : "", v ? "error" : ""); }

  async function enterVehicles() {
    await loadCameras();
    if (!V.cfg || !V.dirty) {
      const d = await api("GET", "/api/vehicles");
      V.cfg = d.config;
    }
    renderVehicles();
  }
  S.leaveHooks.vehicles = () => { Object.values(V.previews).forEach((p) => clearInterval(p.timer)); V.previews = {}; };

  function renderVehicles() {
    const cfg = V.cfg;
    $("#vehCount").replaceChildren(...[1, 2, 3, 4].map((n) => el("button", {
      type: "button", class: "seg-btn", "aria-pressed": cfg.count === n ? "true" : "false", text: String(n),
      onclick: () => { cfg.count = n; setVehDirty(true); renderVehicles(); },
    })));
    const det = cfg.detection;
    $("#detMotion").checked = det.require_motion_to_start;
    $("#detTrack").checked = det.limit_to_track;
    $("#detWidth").value = String(det.process_max_width);
    $("#detLost").value = det.lost_after_s;
    $("#detLedH").value = U.toInput(det.led_height_mm != null ? det.led_height_mm : 50);
    Object.values(V.previews).forEach((p) => clearInterval(p.timer));
    V.previews = {};
    $("#vehCards").replaceChildren(...cfg.vehicles.slice(0, cfg.count).map((v, i) => vehicleCard(v, i)));
    renderVehWarnings();
  }
  $("#detMotion").addEventListener("change", (e) => { V.cfg.detection.require_motion_to_start = e.target.checked; setVehDirty(true); });
  $("#detTrack").addEventListener("change", (e) => { V.cfg.detection.limit_to_track = e.target.checked; setVehDirty(true); });
  $("#detWidth").addEventListener("change", (e) => { V.cfg.detection.process_max_width = Number(e.target.value); setVehDirty(true); });
  $("#detLost").addEventListener("change", (e) => { V.cfg.detection.lost_after_s = Number(e.target.value); setVehDirty(true); });
  $("#detLedH").addEventListener("change", (e) => { V.cfg.detection.led_height_mm = U.fromInput(e.target.value); setVehDirty(true); });

  function renderVehWarnings(extra = []) {
    const act = V.cfg.vehicles.slice(0, V.cfg.count);
    const w = [...extra];
    for (let a = 0; a < act.length; a++) for (let b = a + 1; b < act.length; b++) {
      const d = hueDist(act[a].color, act[b].color);
      if (d < 40) w.push(`${act[a].name} y ${act[b].name} tienen colores parecidos (${d.toFixed(0)}° de diferencia de tono): las cámaras pueden confundirlos.`);
    }
    act.forEach((v) => { if (hexToHsvCv(v.color)[1] < 80) w.push(`${v.name}: color poco saturado (blanco/gris), difícil de separar del fondo.`); });
    $("#vehWarnings").replaceChildren(...[...new Set(w)].map((t) => el("div", { class: "banner warn", text: t })));
  }

  function vehicleCard(v, i) {
    const sw = el("span", { class: "swatch-big" });
    const paint = () => { sw.style.background = v.color; sw.style.setProperty("--sw", v.color); };
    paint();
    const colorIn = el("input", { type: "color", value: v.color, "aria-label": "Color del LED" });
    const hexIn = el("input", { value: v.color, maxlength: 7, spellcheck: "false", "aria-label": "Color en hexadecimal" });
    const hsvTxt = el("span", { class: "muted small" });
    const showHsv = () => { const [h, s_, val] = hexToHsvCv(v.color); hsvTxt.textContent = `Tono ${h * 2}° · Sat ${s_} · Brillo ${val}`; };
    showHsv();
    const changed = () => { paint(); showHsv(); setVehDirty(true); renderVehWarnings(); refreshPreview(i); };
    const setColor = (c) => { v.color = c.toLowerCase(); colorIn.value = v.color; hexIn.value = v.color; changed(); };
    colorIn.addEventListener("input", () => setColor(colorIn.value));
    hexIn.addEventListener("change", () => { if (/^#[0-9a-f]{6}$/i.test(hexIn.value)) setColor(hexIn.value); else hexIn.value = v.color; });
    const range = (key, label, min, max) => {
      const out = el("output", { text: v[key] });
      const r = el("input", { type: "range", min, max, value: v[key], "aria-label": label });
      r.addEventListener("input", () => { v[key] = Number(r.value); out.textContent = r.value; setVehDirty(true); refreshPreview(i); });
      return el("label", {}, label, el("div", { class: "range-row" }, r, out));
    };
    const numIn = (key, label, min, max) => {
      const n = el("input", { type: "number", min, max, value: v[key] });
      n.addEventListener("change", () => { v[key] = Number(n.value); setVehDirty(true); refreshPreview(i); });
      return el("label", {}, label, n);
    };
    const name = el("input", { value: v.name, maxlength: 30 });
    name.addEventListener("change", () => { v.name = name.value || `Auto ${i + 1}`; setVehDirty(true); renderVehWarnings(); });
    const rfidIn = el("input", { value: v.rfid_uid || "", maxlength: 20, spellcheck: "false",
      placeholder: "ej. 04A1B2C3", "aria-label": "UID de la etiqueta RFID" });
    rfidIn.addEventListener("change", () => {
      v.rfid_uid = rfidIn.value.trim().toUpperCase();
      rfidIn.value = v.rfid_uid;
      setVehDirty(true);
      renderVehWarnings();
    });
    const cams = usable();
    const prevSel = el("select", { "aria-label": "Cámara para la vista previa" }, ...cams.map((c) => el("option", { value: c.id, text: c.name })));
    const prevImg = el("img", { alt: `Detección de ${v.name}` });
    const prevStats = el("div", { class: "stats" });
    const prevBox = el("div", { class: "veh-preview", hidden: true },
      el("div", { class: "video-box" }, prevImg), prevStats);
    const prevBtn = el("button", { class: "btn small ghost", type: "button", text: "Ver detección", disabled: !cams.length });
    prevBtn.addEventListener("click", () => {
      const on = prevBox.hidden;
      prevBox.hidden = !on;
      prevBtn.textContent = on ? "Ocultar detección" : "Ver detección";
      if (on) startPreview(i, prevSel, prevImg, prevStats); else stopPreview(i);
    });
    prevSel.addEventListener("change", () => { if (!prevBox.hidden) startPreview(i, prevSel, prevImg, prevStats); });
    return el("div", { class: "panel veh-card" },
      el("div", { class: "panel-title" }, sw, el("span", { text: `Vehículo ${i + 1}` }), el("span", { class: "spacer" }), hsvTxt),
      el("div", { class: "form-grid" },
        el("label", { class: "wide" }, "Nombre", name),
        el("label", { class: "wide" }, "UID de la etiqueta RFID (opcional, para el semáforo/lectores)", rfidIn),
        el("label", { class: "wide" }, "Color del LED",
          el("div", { class: "color-row" }, colorIn, hexIn),
          el("div", { class: "presets" }, ...PRESETS.map(([n, c]) => el("button", {
            type: "button", class: "preset", title: n, style: `background:${c}`, onclick: () => setColor(c) })))),
        range("hue_tol", "Tolerancia de tono (±)", 2, 45),
        range("sat_min", "Saturación mínima", 0, 255),
        range("val_min", "Brillo mínimo", 0, 255),
        el("div"),
        numIn("area_min_px", "Área mínima (px)", 1, 100000),
        numIn("area_max_px", "Área máxima (px)", 10, 2000000)),
      el("div", { class: "form-actions" },
        el("button", { class: "btn small", type: "button", text: "Tomar color desde cámara", disabled: !cams.length,
          onclick: () => openPicker(i, () => { colorIn.value = hexIn.value = v.color; renderVehicles(); }) }),
        prevBtn, prevSel),
      prevBox);
  }

  function maskUrl(i, cid) {
    const v = V.cfg.vehicles[i];
    const q = new URLSearchParams({ color: v.color, hue_tol: v.hue_tol, sat_min: v.sat_min, val_min: v.val_min,
                                    area_min_px: v.area_min_px, area_max_px: v.area_max_px, t: Date.now() });
    return `/api/vehicles/${i + 1}/mask/${cid}.jpg?${q}`;
  }
  async function loadPreview(i) {
    const p = V.previews[i];
    if (!p || p.busy) return;
    p.busy = true;
    try {
      const r = await fetch(maskUrl(i, p.sel.value));
      if (!r.ok) {
        let msg = `Error ${r.status}`;
        try { msg = (await r.json()).error || msg; } catch (_) { /* sin JSON */ }
        p.stats.textContent = msg;
      } else {
        const st = JSON.parse(r.headers.get("X-Mask-Stats") || "{}");
        const url = URL.createObjectURL(await r.blob());
        if (p.url) URL.revokeObjectURL(p.url);
        p.url = url; p.img.src = url;
        p.stats.textContent = `blobs válidos: ${st.valid_blobs} · mayor: ${st.largest_area_px} px · píxeles del color: ${st.matched_pixels}`;
      }
    } catch (e) { p.stats.textContent = e.message; }
    p.busy = false;
  }
  function startPreview(i, sel, img, stats) {
    stopPreview(i);
    V.previews[i] = { sel, img, stats, timer: setInterval(() => loadPreview(i), 800) };
    loadPreview(i);
  }
  function stopPreview(i) { const p = V.previews[i]; if (p) { clearInterval(p.timer); if (p.url) URL.revokeObjectURL(p.url); delete V.previews[i]; } }
  function refreshPreview(i) { if (V.previews[i]) loadPreview(i); }

  // ---- tomar color: se muestrea la MISMA imagen que se ve (en el navegador)
  let pickDone = null;
  function openPicker(i, done) {
    V.pickFor = i; pickDone = done;
    const sel = $("#pickCam");
    sel.replaceChildren(...usable().map((c) => el("option", { value: c.id, text: c.name })));
    $("#pickTitle").textContent = `Tomar color · ${V.cfg.vehicles[i].name}`;
    $("#pickMarker").hidden = true;
    setMsg($("#pickMsg"), "");
    loadPickImage();
    $("#pickDialog").showModal();
  }
  function loadPickImage() {
    const img = $("#pickImg");
    setMsg($("#pickMsg"), "Cargando imagen…");
    img.onload = () => setMsg($("#pickMsg"), "Hacé clic sobre el LED");
    img.onerror = () => setMsg($("#pickMsg"), "No llegó imagen de la cámara", "error");
    img.src = `/api/cameras/${$("#pickCam").value}/snapshot.jpg?t=${Date.now()}`;
  }
  $("#pickCam").addEventListener("change", loadPickImage);
  $("#pickRefresh").addEventListener("click", loadPickImage);
  $("#pickImg").addEventListener("click", (e) => {
    const img = e.currentTarget;
    if (!img.naturalWidth) return;
    const x = Math.round(e.offsetX * img.naturalWidth / img.clientWidth);
    const y = Math.round(e.offsetY * img.naturalHeight / img.clientHeight);
    const cv = document.createElement("canvas");
    cv.width = img.naturalWidth; cv.height = img.naturalHeight;
    const ctx = cv.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    const r = 6, x0 = Math.max(0, x - r), y0 = Math.max(0, y - r);
    const data = ctx.getImageData(x0, y0, Math.min(cv.width - x0, 2 * r + 1), Math.min(cv.height - y0, 2 * r + 1)).data;
    let all = [];
    for (let k = 0; k < data.length; k += 4) {
      const hex = "#" + [data[k], data[k + 1], data[k + 2]].map((u) => u.toString(16).padStart(2, "0")).join("");
      all.push(hexToHsvCv(hex));
    }
    let sel = all.filter(([, s_, v_]) => s_ >= 60 && v_ >= 60);
    const colored = sel.length >= 3;
    if (!colored) sel = all;
    const ang = sel.map(([h]) => h * Math.PI / 90);
    let hm = Math.atan2(ang.reduce((a, t) => a + Math.sin(t), 0), ang.reduce((a, t) => a + Math.cos(t), 0)) * 90 / Math.PI;
    hm = ((hm % 180) + 180) % 180;
    const med = (arr) => { const a = [...arr].sort((p, q) => p - q); return a[Math.floor(a.length / 2)]; };
    const sm = med(sel.map((p) => p[1])), vm = med(sel.map((p) => p[2]));
    const v = V.cfg.vehicles[V.pickFor];
    v.color = hsvCvToHex(Math.round(hm), sm, vm);
    v.sat_min = Math.max(40, Math.min(200, Math.round(sm * 0.55)));
    v.val_min = Math.max(40, Math.min(200, Math.round(vm * 0.55)));
    const mk = $("#pickMarker");
    mk.hidden = false; mk.style.left = `${e.offsetX}px`; mk.style.top = `${e.offsetY}px`;
    mk.style.background = v.color;
    setMsg($("#pickMsg"), colored
      ? `Color tomado ${v.color} (tono ${Math.round(hm) * 2}°, sat ${sm}, brillo ${vm}). Saturación mínima → ${v.sat_min}, brillo mínimo → ${v.val_min}.`
      : `Ahí no hay color saturado (¿centro blanco del LED o fondo?). Probá en el borde del LED. Se tomó ${v.color}.`, colored ? "ok" : "error");
    setVehDirty(true);
    if (pickDone) pickDone();
  });

  $("#btnVehSave").addEventListener("click", async () => {
    const btn = $("#btnVehSave");
    btn.disabled = true;
    try {
      const d = await api("PUT", "/api/vehicles", V.cfg);
      V.cfg = d.config;
      setVehDirty(false);
      setMsg($("#vehMsg"), "Guardado", "ok");
      renderVehicles();
      renderVehWarnings(d.warnings);
      toast("Vehículos guardados");
    } catch (err) {
      const f = (err.data && err.data.fields) || {};
      const t = Object.entries(f).map(([k, v]) => `${k}: ${v}`).join(" · ") || err.message;
      setMsg($("#vehMsg"), t, "error");
      toast(t, true);
    } finally { btn.disabled = false; }
  });

  // ================================================================ 6. TRACKING
  const K = { timer: null, ed: null, st: null, camsKey: "" };

  async function enterTracking(autostart = false) {
    await loadCameras();
    if (!T.track) {
      T.track = (await api("GET", "/api/track")).track;
      T.track.calibration = T.track.calibration || { points: [], homographies: {} };
    }
    if (!K.ed) K.ed = new TrackEditor($("#trkCanvas"), { undoStacks: new Map() });
    K.ed.setView({
      key: "trk", kind: "plan", static: true, width: T.track.width_mm, height: T.track.height_mm, unit: U.view(),
      gridMm: T.track.grid_mm, snap: false, image: null, layers: T.track.plan, overlay: drawVehicles,
    });
    K.ed.active = { layer: "none", zone: -1 };
    clearInterval(K.timer);
    if (autostart) {
      try { await api("POST", "/api/tracking/start"); } catch (e) { toast(e.message, true); }
    }
    await pollTracking();
    K.timer = setInterval(pollTracking, 150);
  }
  S.leaveHooks.tracking = () => { clearInterval(K.timer); K.timer = null; $$("#trkCams img").forEach(detachStream); K.camsKey = ""; };

  async function pollTracking() {
    let st;
    try { st = await api("GET", "/api/tracking/state"); } catch (_) { return; }
    K.st = st;
    const chip = $("#trkState");
    chip.className = "chip " + (st.demo ? "warn" : st.running ? "ok" : "off");
    chip.textContent = st.demo ? "🧪 DEMO" : st.running ? "En vivo" : "Detenido";
    $("#btnTrkToggle").textContent = st.running ? "■ Detener" : "▶ Iniciar tracking";
    $("#btnTrkToggle").disabled = !!st.dependency_error;
    $("#btnTrkDemo").textContent = st.demo ? "■ Detener demo" : "🧪 Modo demo";
    $("#btnGoTracking").classList.toggle("on", st.running);
    $("#trkUptime").textContent = st.running ? `activo hace ${st.uptime_s} s` : "";
    const warns = [st.dependency_error, ...(st.running ? st.warnings : [])].filter(Boolean);
    if (!st.running && !st.dependency_error) warns.push("El tracking está detenido. Tocá «Iniciar tracking» o probá el «Modo demo».");
    $("#trkWarnings").replaceChildren(...warns.map((w) => el("div", { class: "banner warn", text: w })));
    const seen = st.vehicles.filter((v) => v.state !== "buscando").length;
    $("#trkInfo").textContent = `${seen}/${st.vehicles.length} vehículos ubicados` + (st.centerline_mm ? ` · línea central ${st.centerline_mm} mm` : "");
    renderStandings(st);
    renderTrkCams(st);
    K.ed.render();
  }

  function fmtLap(s) { return s == null ? "—" : `${s.toFixed(2)} s`; }
  const STATE_CLS = { siguiendo: "ok", perdido: "warn", buscando: "off" };

  function renderStandings(st) {
    const rows = [...st.vehicles].sort((a, b) => (a.position || 99) - (b.position || 99) || a.id - b.id);
    $("#trkTable tbody").replaceChildren(...rows.map((v) => el("tr", {},
      el("td", { class: "pos", text: v.position ? `${v.position}°` : "—" }),
      el("td", {}, el("span", { class: "car" }, el("span", { class: "dot", style: `background:${v.color};box-shadow:0 0 6px ${v.color}` }), v.name)),
      el("td", {}, el("span", { class: "chip " + (STATE_CLS[v.state] || "off"), text: v.state })),
      el("td", { class: "mono", text: String(v.laps) }),
      el("td", { class: "mono nowrap" }, v.speed_m_s != null ? `${v.speed_m_s.toFixed(2)} m/s` : "—",
        v.speed_m_s != null ? el("div", { class: "muted small", text: `${Math.round(v.speed_scale_kmh)} km/h esc.` }) : null),
      el("td", { class: "mono", text: `${fmtLap(v.last_lap_s)} / ${fmtLap(v.best_lap_s)}` }),
      el("td", { class: "mono", text: v.cams.length ? v.cams.join(", ") : "—" }))));
  }

  function renderTrkCams(st) {
    const box = $("#trkCams");
    const ids = Object.keys(st.cameras || {});
    const show = $("#trkShowCams").checked;
    const key = ids.join(",") + "|" + show + "|" + st.running;
    if (key !== K.camsKey) {
      $$("#trkCams img").forEach(detachStream);
      K.camsKey = key;
      if (!ids.length) { box.replaceChildren(el("div", { class: "empty", text: "Sin cámaras procesando. Iniciá el tracking." })); return; }
      box.replaceChildren(...ids.map((cid) => {
        const c = st.cameras[cid];
        const tile = el("div", { class: "panel live-tile", "data-trk": cid },
          el("div", { class: "panel-title" }, el("span", { text: c.name }), el("span", { class: "spacer" }), el("span", { class: "chip", "data-k": "cal" })),
          show ? el("div", { class: "video-box" }, el("img", { alt: `Detecciones de ${c.name}` })) : null,
          el("div", { class: "metrics" }, metric("FPS procesados", "fps"), metric("ms por frame", "ms"),
            metric("Frames salteados", "skip"), metric("Detecciones", "det"),
            el("div", { class: "metric wide", hidden: true, "data-m": "errbox" }, el("div", { class: "k", text: "Aviso" }), el("div", { class: "v err", "data-v": "err" }))));
        const img = $("img", tile);
        if (img) { img.dataset.on = "1"; img.src = `/api/tracking/cameras/${cid}/debug.mjpg?t=${Date.now()}`; }
        return tile;
      }));
    }
    for (const cid of ids) {
      const c = st.cameras[cid], tile = $(`[data-trk="${cid}"]`);
      if (!tile) continue;
      const set = (k, v) => { const n = $(`[data-v="${k}"]`, tile); if (n) n.textContent = v; };
      set("fps", c.fps_processed); set("ms", c.proc_ms); set("skip", c.frames_skipped);
      set("det", c.detections.length ? c.detections.map((d) => `#${d.vehicle}`).join(" ") : "—");
      const cal = $('[data-k="cal"]', tile);
      cal.className = "chip " + (c.calibrated ? "ok" : "warn");
      cal.textContent = c.calibrated ? `calibrada · ${U.fmt(c.rms_mm, 1)}${c.camera_height_mm ? ` · cámara a ${(c.camera_height_mm / 1000).toFixed(2)} m` : ""}` : "sin calibrar";
      const eb = $('[data-m="errbox"]', tile);
      eb.hidden = !c.last_error; set("err", c.last_error || "");
    }
  }

  function drawVehicles(ctx, ed) {
    const st = K.st;
    if (!st) return;
    for (const v of st.vehicles) {
      if (v.x == null) continue;
      if (v.trail && v.trail.length > 1) {
        ctx.strokeStyle = v.color; ctx.lineWidth = 3; ctx.globalAlpha = 0.45; ctx.lineJoin = "round";
        ctx.beginPath();
        v.trail.forEach((p, k) => { const [sx, sy] = ed.toScreen(p[0], p[1]); k ? ctx.lineTo(sx, sy) : ctx.moveTo(sx, sy); });
        ctx.stroke(); ctx.globalAlpha = 1;
      }
      const [sx, sy] = ed.toScreen(v.x, v.y);
      ctx.save();
      ctx.shadowColor = v.color; ctx.shadowBlur = v.state === "siguiendo" ? 16 : 0;
      ctx.beginPath(); ctx.arc(sx, sy, 11, 0, Math.PI * 2);
      if (v.state === "siguiendo") { ctx.fillStyle = v.color; ctx.fill(); }
      ctx.restore();
      ctx.lineWidth = 2.5; ctx.strokeStyle = "#fff"; ctx.setLineDash(v.state === "siguiendo" ? [] : [4, 3]);
      ctx.beginPath(); ctx.arc(sx, sy, 11, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
      if (v.position) {
        ctx.fillStyle = "#000"; ctx.font = "800 12px system-ui, sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(String(v.position), sx, sy + 0.5);
      }
      const label = `${v.name}${v.speed_m_s != null ? " · " + v.speed_m_s.toFixed(2) + " m/s" : ""}`;
      ctx.font = "600 12px system-ui, sans-serif"; ctx.textAlign = "left";
      const w = ctx.measureText(label).width;
      ctx.fillStyle = "rgba(0,0,0,.78)"; ctx.fillRect(sx + 15, sy - 21, w + 14, 18);
      ctx.fillStyle = v.color; ctx.fillRect(sx + 15, sy - 21, 4, 18);
      ctx.fillStyle = "#fff"; ctx.fillText(label, sx + 24, sy - 12);
    }
  }

  $("#btnTrkToggle").addEventListener("click", async () => {
    const btn = $("#btnTrkToggle");
    btn.disabled = true;
    try {
      if (K.st && K.st.running) await api("POST", "/api/tracking/stop");
      else await api("POST", "/api/tracking/start");
    } catch (e) { toast(e.message, true); }
    btn.disabled = false;
    pollTracking();
  });
  $("#btnTrkDemo").addEventListener("click", async () => {
    const btn = $("#btnTrkDemo");
    btn.disabled = true;
    try {
      if (K.st && K.st.demo) await api("POST", "/api/tracking/stop");
      else { await api("POST", "/api/tracking/demo/start"); toast("Modo demo iniciado: los datos de abajo son simulados"); }
    } catch (e) { toast(e.message, true); }
    btn.disabled = false;
    pollTracking();
  });
  $("#btnTrkReset").addEventListener("click", async () => { await api("POST", "/api/tracking/reset"); toast("Vueltas en cero"); });
  $("#trkShowCams").addEventListener("change", () => { K.camsKey = ""; if (K.st) renderTrkCams(K.st); });
  $("#btnGoTracking").addEventListener("click", () => {
    if (S.section === "tracking") return;
    if (S.section && S.leaveHooks[S.section]) S.leaveHooks[S.section]();
    S.section = "tracking";
    for (const sec of $$(".section")) sec.hidden = sec.id !== "sec-tracking";
    for (const t of $$(".tab")) t.setAttribute("aria-selected", t.dataset.section === "tracking" ? "true" : "false");
    history.replaceState(null, "", HASH.tracking);
    enterTracking(true).catch((e) => toast(e.message, true));
  });

  // ================================================================ 7. ESP32 Y SEMÁFORO
  // Mismo patrón de actualización que el resto de la página (polling con setInterval + fetch;
  // no hay SSE/WebSockets en ningún otro lado de este proyecto, así que no se suma uno acá).
  const ESP_RACE_LABEL = {
    idle: ["Reposo", "off"], countdown: ["Cuenta regresiva", "warn"], running: ["¡Carrera en curso!", "ok"],
    cancelled: ["Cancelada", "off"], error: ["Error", "error"],
  };
  const E = { timer: null, hwLoaded: false };

  async function enterEsp32() {
    if (!E.hwLoaded) {
      try {
        const d = await api("GET", "/api/hardware");
        $("#espMode").value = d.hardware.esp32.mode;
        $("#espPort").value = d.hardware.esp32.port;
        $("#espBaud").value = String(d.hardware.esp32.baudrate);
        E.hwLoaded = true;
      } catch (e) { toast(e.message, true); }
    }
    await pollEsp32();
    clearInterval(E.timer);
    E.timer = setInterval(pollEsp32, 500);
  }
  S.leaveHooks.esp32 = () => { clearInterval(E.timer); E.timer = null; };

  async function pollEsp32() {
    let st;
    try { st = await api("GET", "/api/esp32/state"); } catch (_) { return; }
    renderEsp32(st);
  }

  function renderEsp32(st) {
    // conexión
    const connChip = $("#espConn");
    if (st.mode === "simulation") { connChip.className = "chip ok"; connChip.textContent = "Simulación"; }
    else if (st.connected) { connChip.className = "chip ok"; connChip.textContent = `Hardware · ${st.port || "COM?"}`; }
    else { connChip.className = "chip error"; connChip.textContent = "Hardware · sin conexión"; }

    const warn = [];
    if (st.dependency_error) warn.push(st.dependency_error);
    if (st.mode === "hardware" && !st.connected && st.error) warn.push(`No se pudo conectar con el ESP32: ${st.error}`);
    $("#espWarnings").replaceChildren(...warn.map((t) => el("div", { class: "banner error", text: t })));

    $("#espId").textContent = st.esp32_id || "—";
    $("#espFw").textContent = st.boot_fw || "—";
    $("#espLastComm").textContent = st.last_comm_ms_ago == null ? "Sin mensajes todavía" : `hace ${(st.last_comm_ms_ago / 1000).toFixed(1)} s`;
    $("#espLedAck").textContent = st.mode === "simulation" ? "— (simulación)"
      : (st.led_ack == null ? "Orden enviada, sin confirmar" : (st.led_confirmed ? "Confirmada por el firmware" : "Orden enviada, sin confirmar"));

    // LEDs
    const leds = [];
    for (let i = 0; i < st.led_count; i++) leds.push(el("span", { class: "led" + ((st.led_mask & (1 << i)) ? " on" : "") }));
    $("#espLeds").replaceChildren(...leds);

    // carrera
    const [label, cls] = ESP_RACE_LABEL[st.race.state] || [st.race.state, "off"];
    $("#espRaceState").className = "chip " + cls;
    $("#espRaceState").textContent = label;
    $("#espCountdown").textContent = st.race.state === "countdown" ? `${st.race.remaining_s} s` : "";
    $("#btnEspStart").disabled = st.race.state === "countdown" || st.race.state === "running"
      || (st.mode === "hardware" && !st.connected);
    $("#btnEspCancel").hidden = st.race.state !== "countdown";

    // lectores
    $("#espReaders").replaceChildren(...st.readers.map((r) => {
      const uidIn = el("input", { placeholder: "UID (vacío = al azar)", maxlength: 20 });
      const simBtn = el("button", { class: "btn small ghost", type: "button", text: "Simular lectura",
        disabled: st.mode !== "simulation",
        onclick: async () => {
          try {
            await api("POST", "/api/esp32/rfid/simulate", { lane: r.lane, uid: uidIn.value.trim() || undefined });
            uidIn.value = "";
            pollEsp32();
          } catch (e) { toast(e.message, true); }
        } });
      const who = r.uid ? (r.vehicle_name ? `${r.vehicle_name} (UID ${r.uid})` : `Etiqueta desconocida (UID ${r.uid})`) : "Sin lecturas todavía";
      return el("div", { class: "panel esp-reader-row" },
        el("div", { class: "r1" }, `Lector ${r.lane}`, r.simulated ? el("span", { class: "chip warn", text: "simulado" }) : null),
        el("div", { class: "r2" }, who, r.ms_ago != null ? ` · hace ${(r.ms_ago / 1000).toFixed(1)} s` : ""),
        el("div", { class: "r3" }, uidIn, simBtn));
    }));
  }

  $("#btnEspStart").addEventListener("click", async () => {
    try { renderEsp32(await api("POST", "/api/esp32/race/start")); toast("Cuenta regresiva iniciada"); }
    catch (e) { toast(e.message, true); }
  });
  $("#btnEspCancel").addEventListener("click", async () => {
    try { await api("POST", "/api/esp32/race/cancel"); toast("Cuenta regresiva cancelada"); pollEsp32(); }
    catch (e) { toast(e.message, true); }
  });
  $("#btnEspReset").addEventListener("click", async () => {
    try { await api("POST", "/api/esp32/race/reset"); toast("Semáforo en reposo (no se tocaron vueltas ni tiempos)"); pollEsp32(); }
    catch (e) { toast(e.message, true); }
  });
  $("#btnEspSaveCfg").addEventListener("click", async () => {
    const btn = $("#btnEspSaveCfg");
    btn.disabled = true;
    try {
      await api("PUT", "/api/hardware", {
        esp32: { mode: $("#espMode").value, port: $("#espPort").value.trim(), baudrate: Number($("#espBaud").value) },
      });
      setMsg($("#espCfgMsg"), "Guardado", "ok");
      toast("Modo/puerto guardados");
      pollEsp32();
    } catch (e) {
      setMsg($("#espCfgMsg"), e.message, "error");
      toast(e.message, true);
    } finally { btn.disabled = false; }
  });

  // ================================================================ unidad de medida (UI)
  $("#unitSelect").value = U.unit;
  function refreshUnitLabels() {
    $$(".unit-lbl").forEach((n) => { n.textContent = U.unit; });
  }
  refreshUnitLabels();
  $("#unitSelect").addEventListener("change", (e) => {
    U.set(e.target.value);
    refreshUnitLabels();
    // Reconvierte lo que esté visible ahora mismo (los datos guardados siguen siendo mm).
    if (S.section === "track" && T.track) { selectTrackView(T.viewKey); renderCalibList(); renderCalibQuality(); }
    if (S.section === "vehicles" && V.cfg) { $("#detLedH").value = U.toInput(V.cfg.detection.led_height_mm); }
    if (S.section === "tracking" && T.track && K.ed) K.ed.updateViewProps({ unit: U.view() });
  });

  // ================================================================ primeros pasos (resumen de seteo)
  async function updateSetupProgress() {
    const box = $("#setupProgress");
    if (!box) return;
    let track = null, veh = null;
    try { track = (await api("GET", "/api/track")).track; } catch (_) { /* se muestra sin ese dato */ }
    try { veh = (await api("GET", "/api/vehicles")).config; } catch (_) { /* se muestra sin ese dato */ }
    const usableCams = S.cameras.filter((c) => c.enabled && c.host);
    const camsOk = usableCams.length > 0;
    const pistaOk = !!(track && track.plan.centerline.points.length >= 2);
    const homs = (track && track.calibration && track.calibration.homographies) || {};
    const calibOk = camsOk && usableCams.every((c) => (homs[c.id] || {}).H_img_to_plan);
    const vehOk = !!(veh && veh.vehicles && veh.vehicles.length);
    const steps = [
      { n: 1, label: "Cámaras", ok: camsOk, go: "cameras" },
      { n: 2, label: "Pista", ok: pistaOk, go: "track" },
      { n: 3, label: "Calibración", ok: calibOk, go: "track" },
      { n: 4, label: "Vehículos", ok: vehOk, go: "vehicles" },
    ];
    box.replaceChildren(...steps.map((s) => el("button", {
      class: "setup-pill" + (s.ok ? " done" : ""), type: "button",
      title: s.ok ? `Listo: ${s.label}` : `Falta: ${s.label}`,
      onclick: () => showSection(s.go),
    }, el("span", { class: "setup-check", text: s.ok ? "✓" : String(s.n) }), ` ${s.label}`)));
  }

  // ================================================================ respaldos
  // Antes de cada "Guardar" el servidor copia la versión anterior del archivo. Esto es
  // independiente de cualquier otro mecanismo de recuperación: siempre queda una salida si
  // algo se guardó mal o se borró de más por error.
  const BACKUP_KIND_LABEL = { cameras: "Cámaras", track: "Pista", vehicles: "Vehículos" };
  let backupsCache = null;
  let backupsFilterKind = "";
  function renderBackupsList() {
    const box = $("#backupsList");
    if (!backupsCache) return;
    const list = backupsFilterKind ? backupsCache.filter((b) => b.kind === backupsFilterKind) : backupsCache;
    if (!list.length) {
      box.replaceChildren(el("div", { class: "muted small",
        text: backupsCache.length ? "No hay respaldos de este tipo todavía." : "Todavía no hay respaldos: se crean solos a partir del próximo «Guardar»." }));
      return;
    }
    box.replaceChildren(...list.map((b) => {
      const label = BACKUP_KIND_LABEL[b.kind] || b.kind;
      const when = new Date(b.mtime).toLocaleString();
      return el("div", { class: "calib-row" },
        el("div", { class: "r1" },
          el("span", { text: `${label} · ${when}` }),
          el("button", { class: "btn small ghost", type: "button", text: "Restaurar", onclick: async (ev) => {
            if (!confirm(`¿Restaurar «${label}» a la versión de ${when}? Se guarda antes una copia de la versión actual, por si hay que deshacerlo.`)) return;
            ev.target.disabled = true;
            try {
              await api("POST", "/api/backups/restore", { file: b.file });
              toast("Restaurado. Recargando la página…");
              setTimeout(() => location.reload(), 700);
            } catch (e) { toast(e.message, true); ev.target.disabled = false; }
          } })));
    }));
  }
  $("#backupsFilter").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-kind]");
    if (!btn) return;
    backupsFilterKind = btn.dataset.kind;
    $$("#backupsFilter button").forEach((b) => b.classList.toggle("active", b === btn));
    renderBackupsList();
  });
  $("#btnBackups").addEventListener("click", async () => {
    const dlg = $("#backupsDialog");
    const box = $("#backupsList");
    backupsFilterKind = "";
    $$("#backupsFilter button").forEach((b) => b.classList.toggle("active", b.dataset.kind === ""));
    box.replaceChildren(el("div", { class: "muted small", text: "Cargando…" }));
    dlg.showModal();
    try { backupsCache = (await api("GET", "/api/backups")).backups; } catch (e) { box.replaceChildren(el("div", { class: "muted small", text: e.message })); return; }
    renderBackupsList();
  });

  // ================================================================ versión
  // Si los archivos de la página cambiaron (actualización) se avisa para recargar; si cambió el
  // código Python, hay que reiniciar el servidor (cerrar la ventana negra y abrir INICIAR_ADMIN.bat).
  const PAGE_VERSION = (document.querySelector('meta[name="mcv-version"]') || {}).content;
  const PAGE_ASSETS = (document.querySelector('meta[name="mcv-assets"]') || {}).content;
  async function checkVersion() {
    let v;
    try { v = await api("GET", "/api/version"); } catch (_) { return; }
    let msg = "";
    let canReload = true;
    if (v.restart_needed) {
      msg = "Se actualizó el programa: cerrá la ventana negra del servidor y abrí INICIAR_ADMIN.bat de nuevo.";
      canReload = false;
    } else if (v.version !== PAGE_VERSION || String(v.assets_v) !== String(PAGE_ASSETS)) {
      msg = `Hay una versión nueva de la página (v${v.version}).`;
    }
    $("#updateBanner").hidden = !msg;
    $("#updateText").textContent = msg;
    $("#updateReload").hidden = !canReload;
  }
  $("#updateReload").addEventListener("click", () => {
    if (T.dirty || V.dirty) { if (!confirm("Hay cambios sin guardar. ¿Recargar igual?")) return; T.dirty = V.dirty = false; }
    location.reload();
  });
  setInterval(() => { if (!document.hidden) checkVersion(); }, 10000);
  checkVersion();

  // acceso de solo lectura para depurar desde la consola del navegador y para pruebas automáticas
  window.mcvAdmin = { get editor() { return T.editor; }, get track() { return T.track; }, get linked() { return T.linked; }, get vehicles() { return V.cfg; }, get tracking() { return K.st; } };

  // ================================================================ inicio
  (async function init() {
    try { await loadCameras(); } catch (err) { toast(err.message, true); }
    refreshGlobal().catch(() => {});
    const name = Object.keys(HASH).find((k) => HASH[k] === location.hash) || "cameras";
    showSection(name, false);
  })();
})();
