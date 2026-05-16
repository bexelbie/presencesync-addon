"""FastAPI web UI + REST API. Serves the HA ingress panel."""
from __future__ import annotations

import asyncio
import logging
import os
import tarfile
import tempfile
import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from findmy import LoginState

from . import state, supervisor
from .coordinator import get as get_coord

log = logging.getLogger("presencesync")
logging.basicConfig(level=os.environ.get("PRESENCESYNC_LOG_LEVEL", "info").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))


async def _auto_configure() -> None:
    """Fill in MQTT broker + home location + anisette URL from HA's Supervisor APIs."""
    s = state.get()

    log.info("auto-configure starting; SUPERVISOR_TOKEN present=%s",
             bool(os.environ.get("SUPERVISOR_TOKEN")))

    # Anisette URL precedence:
    #   1. existing user-set value (sticky across restarts)
    #   2. Supervisor /addons sibling discovery (if creds available)
    #   3. http://homeassistant.local:6969 (works as long as the Anisette add-on
    #      is installed with its default ports: 6969/tcp: 6969 mapping)
    env_default = os.environ.get("PRESENCESYNC_ANISETTE_URL", "http://homeassistant.local:6969")
    discovered_anisette = await supervisor.discover_anisette_url()
    log.info("auto-configure: anisette discovery returned %s (prev=%s, env=%s)",
             discovered_anisette, s.apple.anisette_url, env_default)
    # If the previously-stored URL is the broken default, replace it.
    broken = (s.apple.anisette_url or "").startswith("http://local-anisette")
    if discovered_anisette:
        await state.update(lambda x: setattr(x.apple, "anisette_url", discovered_anisette))
    elif broken or not s.apple.anisette_url:
        await state.update(lambda x: setattr(x.apple, "anisette_url", env_default))
    if not s.mqtt.discovery_prefix:
        await state.update(lambda x: setattr(x.mqtt, "discovery_prefix",
                                             os.environ.get("PRESENCESYNC_DISCOVERY_PREFIX", "homeassistant")))
    if not s.mqtt.state_prefix:
        await state.update(lambda x: setattr(x.mqtt, "state_prefix",
                                             os.environ.get("PRESENCESYNC_STATE_PREFIX", "presencesync")))

    # Always re-discover MQTT creds from Supervisor — these can rotate when the
    # user re-installs Mosquitto, and they're not user-facing config anyway.
    mqtt_info = await supervisor.discover_mqtt()
    if mqtt_info:
        log.info("auto-discovered MQTT: %s:%s (user=%s)", mqtt_info.host, mqtt_info.port, mqtt_info.username or "(anon)")
        def m(x):
            x.mqtt.host = mqtt_info.host
            x.mqtt.port = mqtt_info.port
            x.mqtt.username = mqtt_info.username
            x.mqtt.password = mqtt_info.password
        await state.update(m)

    # Pull lat/lon/radius from HA core only if the user hasn't set their own yet
    if not s.home.latitude:
        home_info = await supervisor.discover_home()
        if home_info:
            log.info("auto-discovered home: %s, %s r=%sm (%s)",
                     home_info.latitude, home_info.longitude, home_info.radius_m, home_info.location_name)
            def m(x):
                x.home.latitude = home_info.latitude
                x.home.longitude = home_info.longitude
                x.home.radius_m = int(home_info.radius_m)
            await state.update(m)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    coord = get_coord()
    await _auto_configure()
    await coord.start()
    log.info("PresenceSync web ready")
    yield
    await coord.stop()


class CollapseSlashesMiddleware:
    """Normalize doubled-up slashes in the request path before routing.

    HA's panel-mode Ingress sometimes forwards requests with a leading '//'
    (the result of stripping the slug prefix but leaving its trailing slash).
    FastAPI/Starlette routes match exact paths and don't equate '//' to '/',
    so we collapse runs of slashes here.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            new_path = re.sub(r"/{2,}", "/", scope.get("path", "")) or "/"
            if new_path != scope.get("path"):
                scope = dict(scope)
                scope["path"] = new_path
                scope["raw_path"] = new_path.encode("utf-8")
        await self.app(scope, receive, send)


app = FastAPI(title="PresenceSync", lifespan=lifespan)
app.add_middleware(CollapseSlashesMiddleware)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Under HA Ingress the page is reached at /api/hassio_ingress/<token>/ and
    # HA strips that prefix before passing the request here. Tell the browser
    # the right <base href> so all relative paths (static assets + fetch())
    # resolve to /api/hassio_ingress/<token>/... not the HA root.
    ingress_path = request.headers.get("X-Ingress-Path", "")
    base_href = ingress_path + "/" if ingress_path and not ingress_path.endswith("/") else (ingress_path or "./")
    # Starlette ≥0.29 requires `request` as the first positional arg to
    # TemplateResponse — passing it inside the context dict triggers a
    # TypeError ("unhashable type: 'dict'") deep in jinja2's cache.
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "version": getattr(__import__("presencesync"), "__version__", "?"),
            "base_href": base_href,
        },
    )


@app.get("/api/supervisor-debug")
async def supervisor_debug():
    """Dump what Supervisor tells us — for diagnosing addon discovery."""
    import aiohttp
    token = supervisor.TOKEN
    headers = {"Authorization": f"Bearer {token}"}
    # Include the env vars HA might be using to inject the token
    env_keys = sorted(k for k in os.environ.keys()
                      if any(p in k.upper() for p in ("TOKEN", "HASSIO", "SUPERVISOR", "INGRESS")))
    out = {
        "token_present": bool(token),
        "env_with_token_keys": env_keys,
        "addons": None,
        "info": None,
        "error": None,
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get("http://supervisor/addons", headers=headers) as r:
                out["addons_status"] = r.status
                try:
                    payload = await r.json()
                    addons = (payload.get("data") or {}).get("addons") or []
                    out["addons"] = [
                        {"slug": a.get("slug"), "name": a.get("name"),
                         "hostname": a.get("hostname"), "state": a.get("state"),
                         "version": a.get("version")}
                        for a in addons
                    ]
                except Exception:
                    out["addons"] = await r.text()
            async with s.get("http://supervisor/info", headers=headers) as r:
                out["info_status"] = r.status
                try:
                    out["info"] = await r.json()
                except Exception:
                    out["info"] = await r.text()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


@app.get("/api/ingress-debug")
async def ingress_debug(request: Request):
    """Echo selected headers + URL parts so we can debug Ingress path issues."""
    return {
        "url_path": request.url.path,
        "headers": {
            "x_ingress_path": request.headers.get("X-Ingress-Path"),
            "host": request.headers.get("Host"),
            "x_forwarded_proto": request.headers.get("X-Forwarded-Proto"),
            "x_forwarded_host": request.headers.get("X-Forwarded-Host"),
        },
    }


@app.get("/api/status")
async def status():
    coord = get_coord()
    s = state.get()
    return {
        "apple": {
            "username": s.apple.username,
            "anisette_url": s.apple.anisette_url,
            "login_state": str(coord.apple.last_login_state),
            "beacons_loaded": len(coord.apple.accessories),
        },
        "mqtt": {
            "host": s.mqtt.host,
            "port": s.mqtt.port,
            "connected": coord.mqtt.connected,
            "discovery_prefix": s.mqtt.discovery_prefix,
            "state_prefix": s.mqtt.state_prefix,
        },
        "home": asdict(s.home),
        "tracking": asdict(s.tracking),
        "bundle_uploaded": s.bundle_uploaded,
        "last_run_unix": coord.last_run_unix,
        "last_fixes": [
            {
                "identifier": f.identifier,
                "name": f.name,
                "model": f.model,
                "latitude": f.latitude,
                "longitude": f.longitude,
                "horizontal_accuracy": f.horizontal_accuracy,
                "timestamp_unix": f.timestamp_unix,
            }
            for f in coord.last_fixes
        ],
    }


@app.get("/api/health")
async def health():
    """Structured health check for the UI dashboard."""
    import aiohttp
    import time as _time
    coord = get_coord()
    s = state.get()
    login_state = str(coord.apple.last_login_state)

    # Apple
    if "LOGGED_IN" in login_state:
        apple = {"status": "healthy",
                 "detail": f"Logged in as {s.apple.username or '?'}"}
    elif "REQUIRE_2FA" in login_state:
        apple = {"status": "needs_2fa",
                 "detail": "Apple sent a 6-digit code to your trusted devices — enter it below"}
    elif not s.apple.username:
        apple = {"status": "needs_login",
                 "detail": "Apple ID not configured"}
    else:
        apple = {"status": "needs_login",
                 "detail": f"Not logged in (state={login_state}). Sign in again."}

    # MQTT
    if coord.mqtt.connected:
        mqtt_h = {"status": "healthy",
                  "detail": f"Connected to {s.mqtt.host}:{s.mqtt.port}"}
    else:
        mqtt_h = {"status": "disconnected",
                  "detail": f"Not connected to {s.mqtt.host}:{s.mqtt.port} — check broker credentials and that the Mosquitto add-on is running"}

    # Anisette — probe the URL
    anisette_url = s.apple.anisette_url
    anisette = {"status": "unknown", "detail": "not tested"}
    if anisette_url:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as sess:
                async with sess.get(anisette_url) as r:
                    if r.status == 200:
                        anisette = {"status": "healthy",
                                    "detail": f"Reachable at {anisette_url}"}
                    else:
                        anisette = {"status": "unreachable",
                                    "detail": f"{anisette_url} returned HTTP {r.status}"}
        except Exception as err:
            anisette = {"status": "unreachable",
                        "detail": f"{anisette_url} not reachable: {type(err).__name__}. Make sure the PresenceSync Anisette add-on is installed and started."}
    else:
        anisette = {"status": "unreachable",
                    "detail": "No anisette URL configured"}

    # Bundle
    n_acc = len(coord.apple.accessories)
    if s.bundle_uploaded and n_acc:
        bundle_h = {"status": "healthy",
                    "detail": f"{n_acc} item(s) loaded"}
    elif s.bundle_uploaded and not n_acc:
        bundle_h = {"status": "needs_upload",
                    "detail": "Bundle marked uploaded but no items loaded — re-upload the bundle"}
    else:
        bundle_h = {"status": "needs_upload",
                    "detail": "Run the extractor on your Mac and upload presencesync-bundle.tar.gz"}

    # iCloud (family + owned Apple devices)
    ic_state = coord.icloud.login_state
    if ic_state == "logged_in":
        icloud_h = {"status": "healthy", "detail": f"{len(coord.last_device_fixes)} device(s) reporting"}
    elif ic_state == "needs_2fa":
        icloud_h = {"status": "needs_2fa", "detail": "iCloud needs a 6-digit 2FA code"}
    elif ic_state == "logged_out":
        icloud_h = {"status": "needs_login", "detail": "iCloud (family + devices) not authenticated"}
    else:
        icloud_h = {"status": "needs_login", "detail": f"iCloud state: {ic_state}"}

    # Items (most recent state) — include the home/away resolution so the
    # dashboard can show it without re-implementing haversine in JS.
    import math
    def _haversine_m(lat1, lon1, lat2, lon2):
        r = 6371000.0
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(math.sqrt(a))

    items = []
    def _make_item(name, model, lat, lon, acc, ts, ident, kind):
        if s.home.latitude:
            dist = _haversine_m(lat, lon, s.home.latitude, s.home.longitude)
            state_val = "home" if dist <= s.home.radius_m else "away"
        else:
            dist = None
            state_val = "unknown"
        return {
            "identifier": ident, "name": name, "model": model,
            "latitude": lat, "longitude": lon, "horizontal_accuracy": acc,
            "timestamp_unix": ts, "state": state_val,
            "distance_from_home_m": dist, "kind": kind,
        }

    for f in coord.last_fixes:
        items.append(_make_item(f.name, f.model, f.latitude, f.longitude,
                                f.horizontal_accuracy, f.timestamp_unix,
                                f.identifier, "airtag"))
    for d in coord.last_device_fixes:
        items.append(_make_item(d.name, d.model, d.latitude, d.longitude,
                                d.horizontal_accuracy, d.timestamp_unix,
                                d.identifier, "device"))

    # Overall summary — iCloud is optional (warn if not healthy but don't degrade overall)
    must_be_healthy = (apple, mqtt_h, anisette, bundle_h)
    overall = "healthy" if all(x["status"] == "healthy" for x in must_be_healthy) else "degraded"

    return {
        "overall": overall,
        "now_unix": int(_time.time()),
        "last_poll_unix": coord.last_run_unix,
        "apple": apple,
        "icloud": icloud_h,
        "mqtt": mqtt_h,
        "anisette": anisette,
        "bundle": bundle_h,
        "items": items,
        "anisette_url": s.apple.anisette_url,
        "apple_username": s.apple.username,
    }


@app.post("/api/mqtt")
async def set_mqtt(body: dict):
    def m(s):
        s.mqtt.host = body.get("host", s.mqtt.host)
        s.mqtt.port = int(body.get("port", s.mqtt.port))
        s.mqtt.username = body.get("username", s.mqtt.username)
        s.mqtt.password = body.get("password", s.mqtt.password)
        if body.get("discovery_prefix"):
            s.mqtt.discovery_prefix = body["discovery_prefix"]
        if body.get("state_prefix"):
            s.mqtt.state_prefix = body["state_prefix"]
    await state.update(m)
    await get_coord().reload_mqtt()
    return {"ok": True}


@app.post("/api/home")
async def set_home(body: dict):
    def m(s):
        s.home.latitude = float(body.get("latitude", s.home.latitude))
        s.home.longitude = float(body.get("longitude", s.home.longitude))
        s.home.radius_m = int(body.get("radius_m", s.home.radius_m))
    await state.update(m)
    return {"ok": True}


@app.post("/api/apple/login")
async def apple_login(body: dict):
    """Authenticate to BOTH backends with the same Apple ID:
       - findmy.py (gateway.icloud.com) for AirTags
       - pyicloud  (fmipmobile.icloud.com) for family + own iPhone/iPad/Mac/Watch
    Apple sends 2FA codes per session; same 6-digit code typically validates
    both because they're within the validity window. /api/apple/2fa/submit
    applies the user's code to both.
    """
    username = body.get("username") or ""
    password = body.get("password") or ""
    anisette = body.get("anisette_url") or state.get().apple.anisette_url
    if not username or not password:
        raise HTTPException(400, "username and password required")
    def m(s):
        s.apple.username = username
        s.apple.password = password
        s.apple.anisette_url = anisette
    await state.update(m)
    coord = get_coord()

    # ─── findmy.py login (AirTags) ───────────────────────────────────────
    coord.apple.account = None
    coord.apple.anisette = None
    state.clear_apple_state()
    await coord.apple.ensure_account()
    findmy_state = "ERROR"
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            findmy_state = str(await coord.apple.login(username, password))
            last_err = None
            break
        except (asyncio.TimeoutError, TimeoutError) as e:
            last_err = e
            log.warning("findmy.py login timed out (attempt %d/3) — retrying", attempt + 1)
            # Reset and try again — Apple's gateway is occasionally slow on the
            # mobileme handshake; same credentials usually work on the 2nd try.
            coord.apple.account = None
            await coord.apple.ensure_account()
        except Exception as e:
            last_err = e
            log.exception("findmy.py login raised (attempt %d/3)", attempt + 1)
            break
    if last_err and findmy_state == "ERROR":
        findmy_state = f"ERROR: {type(last_err).__name__}: {last_err}"

    # ─── pyicloud login (family + owned Apple devices) ───────────────────
    icloud_state = "ERROR"
    try:
        icloud_state = await asyncio.get_event_loop().run_in_executor(
            None, coord.icloud.login, username, password
        )
    except Exception as e:
        log.exception("pyicloud login raised")
        icloud_state = f"ERROR: {type(e).__name__}: {e}"

    return {"login_state": findmy_state, "findmy_state": findmy_state,
            "icloud_state": icloud_state}


@app.post("/api/apple/2fa/request")
async def apple_request_2fa(body: dict):
    coord = get_coord()
    method = int(body.get("method", 0))
    try:
        await coord.apple.request_2fa(method)
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    return {"ok": True}


@app.post("/api/apple/2fa/submit")
async def apple_submit_2fa(body: dict):
    """Submit the 2FA code to BOTH backends (same code typically works for both).

    Handles the case where one backend's login timed out before reaching
    REQUIRE_2FA — retries that backend's login first, then submits the code.
    """
    code = body.get("code") or ""
    if not code:
        raise HTTPException(400, "code required")
    coord = get_coord()
    s = state.get()

    # findmy.py — submit if it's in 2FA state, otherwise try to re-login first
    findmy_state = str(coord.apple.last_login_state)
    if "REQUIRE_2FA" not in findmy_state and "LOGGED_IN" not in findmy_state:
        log.info("findmy.py not in 2FA state (was %s) — retrying login before 2FA submit",
                 findmy_state)
        try:
            findmy_state = str(await coord.apple.login(s.apple.username, s.apple.password))
        except Exception as e:
            log.warning("findmy.py re-login failed: %s", e)
            findmy_state = f"ERROR: {type(e).__name__}: {e}"
    if "REQUIRE_2FA" in findmy_state:
        try:
            findmy_state = str(await coord.apple.submit_2fa(code))
        except Exception as e:
            log.warning("findmy.py 2FA submit failed: %s", e)
            findmy_state = f"ERROR: {type(e).__name__}: {e}"

    # pyicloud
    icloud_state = coord.icloud.login_state
    if icloud_state == "needs_2fa":
        try:
            icloud_state = await asyncio.get_event_loop().run_in_executor(
                None, coord.icloud.submit_2fa, code
            )
        except Exception as e:
            log.warning("pyicloud 2FA submit failed: %s", e)
            icloud_state = f"ERROR: {type(e).__name__}: {e}"

    if "LOGGED_IN" in findmy_state or icloud_state == "logged_in":
        return {"login_state": findmy_state, "findmy_state": findmy_state,
                "icloud_state": icloud_state}
    raise HTTPException(500, f"Both 2FAs failed: findmy={findmy_state} icloud={icloud_state}")


@app.post("/api/bundle/upload")
async def upload_bundle(file: UploadFile):
    if not file.filename or not file.filename.endswith((".tar", ".tar.gz", ".tgz")):
        raise HTTPException(400, f"expected a .tar.gz bundle, got filename={file.filename!r}")

    # Save to a temp file, extract into /data/bundle
    bundle_dir = state.BUNDLE_DIR
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # Wipe previous bundle (files only — directory shells are fine)
    for p in bundle_dir.rglob("*"):
        if p.is_file():
            p.unlink()

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
            tmp_path = Path(tmp.name)
            total = 0
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
                total += len(chunk)
        log.info("bundle upload: %s, %d bytes saved to %s", file.filename, total, tmp_path)
        with tarfile.open(tmp_path, "r:*") as t:
            members = t.getmembers()
            log.info("tarball has %d entries: %s%s",
                     len(members),
                     [m.name for m in members[:8]],
                     "…" if len(members) > 8 else "")
            t.extractall(bundle_dir)  # noqa: S202
    except Exception as e:
        log.exception("bundle unpack failed")
        raise HTTPException(400, f"failed to unpack bundle ({type(e).__name__}): {e}")
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except Exception:
                pass

    # What ended up on disk?
    extracted = sorted([str(p.relative_to(bundle_dir)) for p in bundle_dir.rglob("*") if p.is_file()])
    log.info("extracted %d files. first ten: %s", len(extracted), extracted[:10])

    # Load beacons
    coord = get_coord()
    try:
        coord.apple.load_bundle(bundle_dir)
    except Exception as e:
        log.exception("bundle load failed")
        raise HTTPException(400,
            f"bundle is not valid ({type(e).__name__}): {e}. "
            f"Extracted files: {extracted[:20]}{'…' if len(extracted)>20 else ''}"
        )

    await state.update(lambda s: setattr(s, "bundle_uploaded", True))
    return {
        "ok": True,
        "beacons": [
            (a.to_json() if hasattr(a, "to_json") else {"name": str(a)})
            for a in coord.apple.accessories
        ],
    }


@app.post("/api/rediscover")
async def rediscover():
    """Re-pull MQTT + home location + anisette URL from HA Supervisor."""
    mqtt_info = await supervisor.discover_mqtt()
    home_info = await supervisor.discover_home()
    anisette_url = await supervisor.discover_anisette_url()
    def m(x):
        if mqtt_info:
            x.mqtt.host = mqtt_info.host
            x.mqtt.port = mqtt_info.port
            x.mqtt.username = mqtt_info.username
            x.mqtt.password = mqtt_info.password
        if home_info:
            x.home.latitude = home_info.latitude
            x.home.longitude = home_info.longitude
            x.home.radius_m = int(home_info.radius_m)
        if anisette_url:
            x.apple.anisette_url = anisette_url
    await state.update(m)
    if mqtt_info:
        await get_coord().reload_mqtt()
    s = state.get()
    return {
        "mqtt": {"host": s.mqtt.host, "port": s.mqtt.port, "username": s.mqtt.username} if mqtt_info else None,
        "home": {"latitude": s.home.latitude, "longitude": s.home.longitude, "radius_m": s.home.radius_m} if home_info else None,
        "anisette_url": s.apple.anisette_url if anisette_url else None,
    }


@app.post("/api/poll-now")
async def poll_now():
    coord = get_coord()
    fixes = await coord.apple.fetch_locations()
    for f in fixes:
        coord.mqtt.publish_fix(f)
    return {"fixes": len(fixes), "mqtt_connected": coord.mqtt.connected}


@app.post("/api/mqtt-test")
async def mqtt_test():
    """Publish a heartbeat to confirm the broker is reachable + auth works."""
    import time as _time
    coord = get_coord()
    if coord.mqtt._client is None:
        return JSONResponse({"error": "no MQTT client"}, status_code=500)
    topic = f"{state.get().mqtt.state_prefix}/mqtt-test"
    payload = f"ping {_time.time()}"
    info = coord.mqtt._client.publish(topic, payload, qos=1, retain=False)
    return {"topic": topic, "payload": payload, "rc": info.rc, "mid": info.mid,
            "connected": coord.mqtt.connected}


@app.post("/api/reset")
async def reset():
    """Clear all stored state — Apple session + bundle. Keeps MQTT/home config."""
    state.clear_apple_state()
    def m(s):
        s.apple.username = ""
        s.apple.password = ""
        s.bundle_uploaded = False
    await state.update(m)
    coord = get_coord()
    coord.apple.account = None
    coord.apple.accessories = []
    coord.apple.beaconstore_key = None
    return {"ok": True}
