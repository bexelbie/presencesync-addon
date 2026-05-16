const $ = (s) => document.querySelector(s);

async function api(path, opts = {}) {
  const o = { method: opts.method || "GET", headers: {} };
  if (opts.body) {
    o.body = opts.body instanceof FormData ? opts.body : JSON.stringify(opts.body);
    if (!(opts.body instanceof FormData)) o.headers["Content-Type"] = "application/json";
  }
  // Force absolute URL using the current page's pathname as the base. This
  // makes the request work under HA Ingress whether or not <base href> was
  // honoured and whether or not the page URL ended with a trailing slash.
  const pageBase = window.location.pathname.replace(/\/?$/, "/");
  const url = pageBase + path.replace(/^\//, "");
  const r = await fetch(url, o);
  const t = await r.text();
  let data;
  try { data = JSON.parse(t); } catch { data = t; }
  if (!r.ok) throw new Error(typeof data === "string" ? data : JSON.stringify(data));
  return data;
}

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    $("#status-out").textContent = JSON.stringify(s, null, 2);
    if (s.mqtt) {
      if (!$("#mqtt-host").value) $("#mqtt-host").value = s.mqtt.host;
      if (!$("#mqtt-port").value || $("#mqtt-port").value === "1883") $("#mqtt-port").value = s.mqtt.port;
    }
    if (s.home && s.home.latitude) {
      $("#home-lat").value = s.home.latitude;
      $("#home-lon").value = s.home.longitude;
      $("#home-radius").value = s.home.radius_m;
    }
    if (s.apple && s.apple.anisette_url) $("#apple-anisette").value = s.apple.anisette_url;
    if (s.apple && s.apple.username) $("#apple-username").value = s.apple.username;
    if (s.apple && s.apple.login_state === "LoginState.REQUIRE_2FA") $("#setup-2fa").hidden = false;
  } catch (e) {
    $("#status-out").textContent = "status error: " + e.message;
  }
}

$("#save-mqtt").onclick = async () => {
  await api("/api/mqtt", { method: "POST", body: {
    host: $("#mqtt-host").value,
    port: parseInt($("#mqtt-port").value, 10),
    username: $("#mqtt-username").value,
    password: $("#mqtt-password").value,
  }});
  refreshStatus();
};

$("#save-home").onclick = async () => {
  await api("/api/home", { method: "POST", body: {
    latitude: parseFloat($("#home-lat").value),
    longitude: parseFloat($("#home-lon").value),
    radius_m: parseInt($("#home-radius").value, 10),
  }});
  refreshStatus();
};

$("#upload-bundle").onclick = async () => {
  const f = $("#bundle-file").files[0];
  if (!f) return alert("pick a file first");
  const fd = new FormData(); fd.append("file", f);
  try {
    const r = await api("/api/bundle/upload", { method: "POST", body: fd });
    $("#bundle-out").textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    $("#bundle-out").textContent = "upload error: " + e.message;
  }
  refreshStatus();
};

$("#apple-login").onclick = async () => {
  try {
    const r = await api("/api/apple/login", { method: "POST", body: {
      username: $("#apple-username").value,
      password: $("#apple-password").value,
      anisette_url: $("#apple-anisette").value,
    }});
    $("#apple-out").textContent = JSON.stringify(r, null, 2);
    if (r.login_state && r.login_state.includes("REQUIRE_2FA")) {
      $("#setup-2fa").hidden = false;
      await api("/api/apple/2fa/request", { method: "POST", body: { method: 0 }});
    }
  } catch (e) {
    $("#apple-out").textContent = "login error: " + e.message;
  }
  refreshStatus();
};

$("#apple-2fa-submit").onclick = async () => {
  try {
    const r = await api("/api/apple/2fa/submit", { method: "POST", body: { code: $("#apple-2fa-code").value }});
    $("#apple-out").textContent = JSON.stringify(r, null, 2);
  } catch (e) {
    $("#apple-out").textContent = "2fa error: " + e.message;
  }
  refreshStatus();
};

$("#apple-2fa-request").onclick = async () => {
  await api("/api/apple/2fa/request", { method: "POST", body: { method: 0 }});
};

$("#poll-now").onclick = async () => {
  const r = await api("/api/poll-now", { method: "POST" });
  $("#status-out").textContent = "poll fetched " + r.fixes + " fixes";
  setTimeout(refreshStatus, 500);
};

$("#reset").onclick = async () => {
  if (!confirm("Clear Apple session + bundle? (MQTT/home config kept.)")) return;
  await api("/api/reset", { method: "POST" });
  refreshStatus();
};

refreshStatus();
setInterval(refreshStatus, 15000);
