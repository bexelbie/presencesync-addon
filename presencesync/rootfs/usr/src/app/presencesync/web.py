"""Minimal FastAPI setup wizard — Apple login/2FA + AirTag bundle upload.

No dashboard. HA native UI (Lovelace + device_tracker entities) handles
all visualization. This provides only the interactive flows that can't be
done through add-on options alone.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tarfile
import tempfile
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from findmy import LoginState

from . import state, supervisor
from .coordinator import get as get_coord

log = logging.getLogger("presencesync")
logging.basicConfig(level=os.environ.get("PRESENCESYNC_LOG_LEVEL", "info").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def _auto_configure() -> None:
    """Fill in MQTT broker + home location from HA's Supervisor APIs."""
    s = state.get()

    # Clear any stale anisette_url — local provider is used by default now
    if s.apple.anisette_url and s.apple.anisette_url.startswith("http://local"):
        await state.update(lambda x: setattr(x.apple, "anisette_url", ""))

    if not s.mqtt.discovery_prefix:
        await state.update(lambda x: setattr(x.mqtt, "discovery_prefix",
                                             os.environ.get("PRESENCESYNC_DISCOVERY_PREFIX", "homeassistant")))
    if not s.mqtt.state_prefix:
        await state.update(lambda x: setattr(x.mqtt, "state_prefix",
                                             os.environ.get("PRESENCESYNC_STATE_PREFIX", "presencesync")))

    # MQTT creds from Supervisor
    mqtt_info = await supervisor.discover_mqtt()
    if mqtt_info:
        log.info("auto-discovered MQTT: %s:%s", mqtt_info.host, mqtt_info.port)
        def m(x):
            x.mqtt.host = mqtt_info.host
            x.mqtt.port = mqtt_info.port
            x.mqtt.username = mqtt_info.username
            x.mqtt.password = mqtt_info.password
        await state.update(m)

    # Home zone from HA core
    if not s.home.latitude:
        home_info = await supervisor.discover_home()
        if home_info:
            log.info("auto-discovered home: %s, %s r=%sm",
                     home_info.latitude, home_info.longitude, home_info.radius_m)
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
    log.info("PresenceSync ready")
    yield
    await coord.stop()


class _CollapseSlashesMiddleware:
    """Normalize doubled-up slashes from HA Ingress path stripping."""
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
app.add_middleware(_CollapseSlashesMiddleware)


# ─── Status ──────────────────────────────────────────────────────────────────

@app.get("/")
@app.get("/api/status")
async def status():
    """Lightweight status for debugging — replaces the old dashboard."""
    coord = get_coord()
    s = state.get()
    return {
        "version": __import__("presencesync").__version__,
        "findmy_login_state": str(coord.apple.last_login_state),
        "icloud_login_state": coord.icloud.login_state,
        "mqtt_connected": coord.mqtt.connected,
        "airtags_loaded": len(coord.apple.accessories),
        "last_poll_unix": coord.last_run_unix,
        "airtag_fixes": len(coord.last_fixes),
        "device_fixes": len(coord.last_device_fixes),
    }


# ─── Apple Login + 2FA ────────────────────────────────────────────────────────

@app.post("/api/apple/login")
async def apple_login(body: dict):
    """Authenticate to both findmy.py (AirTags) and pyicloud (iDevices)."""
    username = body.get("username") or ""
    password = body.get("password") or ""
    if not username or not password:
        raise HTTPException(400, "username and password required")
    await state.update(lambda s: (
        setattr(s.apple, "username", username),
        setattr(s.apple, "password", password),
    ))
    coord = get_coord()

    # findmy.py (AirTags)
    coord.apple.account = None
    coord.apple.anisette = None
    state.clear_apple_state()
    await coord.apple.ensure_account()
    findmy_state = "ERROR"
    try:
        findmy_state = str(await coord.apple.login(username, password))
    except Exception as e:
        log.exception("findmy.py login failed")
        findmy_state = f"ERROR: {type(e).__name__}: {e}"

    # pyicloud (iDevices)
    icloud_state = "ERROR"
    try:
        icloud_state = await asyncio.get_event_loop().run_in_executor(
            None, coord.icloud.login, username, password
        )
    except Exception as e:
        log.exception("pyicloud login failed")
        icloud_state = f"ERROR: {type(e).__name__}: {e}"

    return {"findmy_state": findmy_state, "icloud_state": icloud_state}


@app.post("/api/apple/2fa/submit")
async def apple_submit_2fa(body: dict):
    """Submit 2FA code to both backends (same code typically works for both)."""
    code = body.get("code") or ""
    if not code:
        raise HTTPException(400, "code required")
    coord = get_coord()
    s = state.get()

    # findmy.py
    findmy_state = str(coord.apple.last_login_state)
    if "REQUIRE_2FA" not in findmy_state and "LOGGED_IN" not in findmy_state:
        try:
            findmy_state = str(await coord.apple.login(s.apple.username, s.apple.password))
        except Exception as e:
            findmy_state = f"ERROR: {type(e).__name__}: {e}"
    if "REQUIRE_2FA" in findmy_state:
        try:
            findmy_state = str(await coord.apple.submit_2fa(code))
        except Exception as e:
            findmy_state = f"ERROR: {type(e).__name__}: {e}"

    # pyicloud
    icloud_state = coord.icloud.login_state
    if icloud_state == "needs_2fa":
        try:
            icloud_state = await asyncio.get_event_loop().run_in_executor(
                None, coord.icloud.submit_2fa, code
            )
        except Exception as e:
            icloud_state = f"ERROR: {type(e).__name__}: {e}"

    if "LOGGED_IN" in findmy_state or icloud_state == "logged_in":
        return {"findmy_state": findmy_state, "icloud_state": icloud_state}
    raise HTTPException(500, f"2FA failed: findmy={findmy_state} icloud={icloud_state}")


# ─── AirTag Bundle Upload ────────────────────────────────────────────────────

def _safe_tar_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract tar members with path traversal protection."""
    dest = dest.resolve()
    for member in tar.getmembers():
        member_path = (dest / member.name).resolve()
        if not str(member_path).startswith(str(dest)):
            raise ValueError(f"Path traversal detected: {member.name}")
        if member.issym() or member.islnk():
            link_target = (dest / member.linkname).resolve()
            if not str(link_target).startswith(str(dest)):
                raise ValueError(f"Symlink traversal detected: {member.name} → {member.linkname}")
    tar.extractall(dest)


@app.post("/api/bundle/upload")
async def upload_bundle(file: UploadFile):
    """Upload AirTag key bundle (presencesync-bundle.tar.gz)."""
    if not file.filename or not file.filename.endswith((".tar", ".tar.gz", ".tgz")):
        raise HTTPException(400, f"expected .tar.gz bundle, got: {file.filename!r}")

    bundle_dir = state.BUNDLE_DIR
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for p in bundle_dir.rglob("*"):
        if p.is_file():
            p.unlink()

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
            tmp_path = Path(tmp.name)
            while chunk := await file.read(1024 * 1024):
                tmp.write(chunk)
        with tarfile.open(tmp_path, "r:*") as t:
            _safe_tar_extract(t, bundle_dir)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"failed to unpack bundle: {type(e).__name__}: {e}")
    finally:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)

    coord = get_coord()
    try:
        coord.apple.load_bundle(bundle_dir)
    except Exception as e:
        raise HTTPException(400, f"bundle invalid: {type(e).__name__}: {e}")

    await state.update(lambda s: setattr(s, "bundle_uploaded", True))
    return {
        "ok": True,
        "accessories": len(coord.apple.accessories),
    }


# ─── Find My Sound ───────────────────────────────────────────────────────────

@app.post("/api/devices/{device_id}/play-sound")
async def play_sound(device_id: str):
    """Trigger Find My alert sound on a device."""
    coord = get_coord()
    success = await asyncio.get_event_loop().run_in_executor(
        None, coord.icloud.play_sound, device_id
    )
    if success:
        return {"status": "ok", "device_id": device_id}
    return JSONResponse({"error": "device not found or not reachable"}, status_code=404)


# ─── Reset ────────────────────────────────────────────────────────────────────

@app.post("/api/reset")
async def reset():
    """Clear Apple sessions + bundle. Keeps MQTT/home config."""
    state.clear_apple_state()
    await state.update(lambda s: (
        setattr(s.apple, "username", ""),
        setattr(s.apple, "password", ""),
        setattr(s, "bundle_uploaded", False),
    ))
    coord = get_coord()
    coord.apple.account = None
    coord.apple.accessories = []
    coord.apple.beaconstore_key = None
    return {"ok": True}
