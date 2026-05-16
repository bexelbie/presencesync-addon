"""FastAPI web UI + REST API. Serves the HA ingress panel."""
from __future__ import annotations

import logging
import os
import tarfile
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from findmy import LoginState

from . import state
from .coordinator import get as get_coord

log = logging.getLogger("presencesync")
logging.basicConfig(level=os.environ.get("PRESENCESYNC_LOG_LEVEL", "info").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(APP_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    coord = get_coord()
    # If anisette URL isn't set in state, default to environment
    s = state.get()
    if not s.apple.anisette_url:
        await state.update(lambda x: setattr(x.apple, "anisette_url",
                                             os.environ.get("PRESENCESYNC_ANISETTE_URL", "")))
    if not s.mqtt.discovery_prefix:
        await state.update(lambda x: setattr(x.mqtt, "discovery_prefix",
                                             os.environ.get("PRESENCESYNC_DISCOVERY_PREFIX", "homeassistant")))
    if not s.mqtt.state_prefix:
        await state.update(lambda x: setattr(x.mqtt, "state_prefix",
                                             os.environ.get("PRESENCESYNC_STATE_PREFIX", "presencesync")))
    await coord.start()
    log.info("PresenceSync web ready")
    yield
    await coord.stop()


app = FastAPI(title="PresenceSync", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Under HA Ingress the page is reached at /api/hassio_ingress/<token>/ and
    # HA strips that prefix before passing the request here. Tell the browser
    # the right <base href> so all relative paths (static assets + fetch())
    # resolve to /api/hassio_ingress/<token>/... not the HA root.
    ingress_path = request.headers.get("X-Ingress-Path", "")
    base_href = ingress_path + "/" if ingress_path and not ingress_path.endswith("/") else (ingress_path or "./")
    return TEMPLATES.TemplateResponse("index.html", {
        "request": request,
        "version": getattr(__import__("presencesync"), "__version__", "?"),
        "base_href": base_href,
    })


@app.get("/api/status")
async def status():
    coord = get_coord()
    s = state.get()
    return {
        "apple": {
            "username": s.apple.username,
            "anisette_url": s.apple.anisette_url,
            "login_state": str(coord.apple.last_login_state),
            "beacons_loaded": len(coord.apple.beacons),
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
    # Force a fresh account with the new anisette
    coord.apple.account = None
    coord.apple.anisette = None
    state.clear_apple_state()
    await coord.apple.ensure_account()
    try:
        result = await coord.apple.login(username, password)
    except Exception as e:
        log.exception("apple login raised")
        raise HTTPException(500, f"login error: {type(e).__name__}: {e}")
    return {"login_state": str(result)}


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
    code = body.get("code") or ""
    if not code:
        raise HTTPException(400, "code required")
    coord = get_coord()
    try:
        result = await coord.apple.submit_2fa(code)
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")
    return {"login_state": str(result)}


@app.post("/api/bundle/upload")
async def upload_bundle(file: UploadFile):
    if not file.filename or not file.filename.endswith((".tar", ".tar.gz", ".tgz")):
        raise HTTPException(400, "expected a .tar.gz bundle")

    # Save to a temp file, extract into /data/bundle
    bundle_dir = state.BUNDLE_DIR
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # Wipe previous bundle
    for p in bundle_dir.rglob("*"):
        if p.is_file():
            p.unlink()

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
            tmp_path = Path(tmp.name)
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                tmp.write(chunk)
        with tarfile.open(tmp_path, "r:*") as t:
            t.extractall(bundle_dir)  # noqa: S202
    except Exception as e:
        raise HTTPException(400, f"failed to unpack bundle: {e}")
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    # Load beacons
    coord = get_coord()
    try:
        coord.apple.load_bundle(bundle_dir)
    except Exception as e:
        raise HTTPException(400, f"bundle is not valid: {e}")

    await state.update(lambda s: setattr(s, "bundle_uploaded", True))
    return {
        "ok": True,
        "beacons": [
            {"identifier": b.identifier, "name": b.name, "model": b.model}
            for b in coord.apple.beacons
        ],
    }


@app.post("/api/poll-now")
async def poll_now():
    coord = get_coord()
    fixes = await coord.apple.fetch_locations()
    for f in fixes:
        coord.mqtt.publish_fix(f)
    return {"fixes": len(fixes)}


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
    coord.apple.beacons = []
    coord.apple.beaconstore_key = None
    return {"ok": True}
