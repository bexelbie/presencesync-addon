const $ = (s) => document.querySelector(s);

async function api(path, opts = {}) {
  const o = { method: opts.method || "GET", headers: {} };
  if (opts.body) {
    o.body = opts.body instanceof FormData ? opts.body : JSON.stringify(opts.body);
    if (!(opts.body instanceof FormData)) o.headers["Content-Type"] = "application/json";
  }
  // <base href> normalizes relative paths, but we strip a leading "/" so a
  // path like "/api/x" still resolves under the ingress prefix.
  const pageBase = window.location.pathname.replace(/\/?$/, "/");
  const url = pageBase + path.replace(/^\//, "");
  const r = await fetch(url, o);
  const t = await r.text();
  let data;
  try { data = JSON.parse(t); } catch { data = t; }
  if (!r.ok) throw new Error(typeof data === "string" ? data : (data.detail || JSON.stringify(data)));
  return data;
}

function setCard(id, statusKind, headerText, bodyText) {
  const card = $("#card-" + id);
  card.classList.remove("healthy", "warn", "err");
  card.classList.add(statusKind);
  card.querySelector(".card-h").textContent = headerText;
  card.querySelector(".card-b").textContent = bodyText;
}

function relTime(unix) {
  if (!unix) return "—";
  const dt = Date.now() / 1000 - unix;
  if (dt < 60) return `${Math.round(dt)}s ago`;
  if (dt < 3600) return `${Math.round(dt / 60)}m ago`;
  if (dt < 86400) return `${Math.round(dt / 3600)}h ago`;
  return `${Math.round(dt / 86400)}d ago`;
}

function statusToCardKind(status) {
  if (status === "healthy") return "healthy";
  if (status === "needs_2fa" || status === "needs_login" || status === "needs_upload") return "warn";
  return "err";
}

async function refresh() {
  let h;
  try { h = await api("/api/health"); }
  catch (e) { $("#overall-pill").textContent = "fetch error"; return; }

  // overall pill
  const pill = $("#overall-pill");
  pill.textContent = h.overall;
  pill.classList.toggle("healthy", h.overall === "healthy");
  pill.classList.toggle("degraded", h.overall !== "healthy");

  // cards
  setCard("apple",    statusToCardKind(h.apple.status),    "Apple",    h.apple.detail);
  setCard("mqtt",     statusToCardKind(h.mqtt.status),     "MQTT",     h.mqtt.detail);
  setCard("anisette", statusToCardKind(h.anisette.status), "Anisette", h.anisette.detail);
  setCard("bundle",   statusToCardKind(h.bundle.status),   "Bundle",   h.bundle.detail);

  // last poll line
  $("#last-poll").textContent = h.last_poll_unix
    ? `Last poll: ${relTime(h.last_poll_unix)}`
    : "Last poll: never";

  // items table
  const tbody = $("#items-tbody");
  tbody.innerHTML = "";
  for (const item of h.items) {
    const tr = document.createElement("tr");
    // home/away — server-side state isn't included here; we'd need to mirror
    // the haversine. Keep "—" for now; HA's zone resolver shows the real state.
    const seen = relTime(item.timestamp_unix);
    tr.innerHTML = `
      <td>${escape(item.name || "?")}</td>
      <td class="muted">${escape(item.model || "—")}</td>
      <td class="state-unknown">—</td>
      <td>${seen}</td>
      <td>±${Math.round(item.horizontal_accuracy)}m</td>`;
    tbody.appendChild(tr);
  }
  $("#items-count").textContent = h.items.length ? `(${h.items.length})` : "";
  $("#items-empty").hidden = h.items.length > 0;

  // Setup sections — hide when each component is healthy
  $("#setup-bundle").hidden = h.bundle.status === "healthy";
  const appleHealthy = h.apple.status === "healthy";
  const needs2fa = h.apple.status === "needs_2fa";
  $("#setup-apple").hidden = appleHealthy || needs2fa;
  $("#setup-2fa").hidden = !needs2fa;

  // Pre-fill known fields
  if (h.apple_username && !$("#apple-username").value) $("#apple-username").value = h.apple_username;
}

function escape(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

// ── form handlers ─────────────────────────────────────────────────────────

$("#upload-bundle").onclick = async () => {
  const f = $("#bundle-file").files[0];
  if (!f) { alert("Pick a file first"); return; }
  const fd = new FormData();
  fd.append("file", f);
  $("#bundle-out").hidden = false;
  $("#bundle-out").textContent = "Uploading…";
  try {
    const r = await api("/api/bundle/upload", { method: "POST", body: fd });
    $("#bundle-out").textContent = `Loaded ${r.beacons.length} items. Bundle saved.`;
  } catch (e) {
    $("#bundle-out").textContent = "Error: " + e.message;
  }
  refresh();
};

$("#apple-login").onclick = async () => {
  $("#apple-out").textContent = "Signing in…";
  try {
    const r = await api("/api/apple/login", { method: "POST", body: {
      username: $("#apple-username").value,
      password: $("#apple-password").value,
    }});
    if (r.login_state && r.login_state.includes("REQUIRE_2FA")) {
      $("#apple-out").textContent = "Sending 6-digit code to your trusted devices…";
      try { await api("/api/apple/2fa/request", { method: "POST", body: { method: 0 }}); } catch {}
    } else if (r.login_state && r.login_state.includes("LOGGED_IN")) {
      $("#apple-out").textContent = "Signed in.";
    } else {
      $("#apple-out").textContent = "Result: " + r.login_state;
    }
  } catch (e) {
    $("#apple-out").textContent = "Error: " + e.message;
  }
  refresh();
};

$("#apple-2fa-submit").onclick = async () => {
  try {
    const r = await api("/api/apple/2fa/submit", { method: "POST", body: { code: $("#apple-2fa-code").value }});
    if (r.login_state && r.login_state.includes("LOGGED_IN")) {
      $("#apple-2fa-code").value = "";
    }
  } catch (e) {
    alert("2FA error: " + e.message);
  }
  refresh();
};

$("#apple-2fa-request").onclick = async () => {
  try { await api("/api/apple/2fa/request", { method: "POST", body: { method: 0 }}); }
  catch (e) { alert("Resend failed: " + e.message); }
};

// kick things off
refresh();
setInterval(refresh, 10000);
