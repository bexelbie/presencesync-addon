# ABOUTME: FastAPI setup wizard for Apple login, 2FA, key extraction, and bundle upload.
# ABOUTME: Serves the ingress UI. All device operations happen via MQTT, not REST.
from __future__ import annotations

import asyncio
import logging
import os
import tarfile
import tempfile
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import state, supervisor
from .coordinator import get as get_coord
from . import extractor as _extractor_mod
from . import anisette_manager

log = logging.getLogger("presencesync")
logging.basicConfig(level=os.environ.get("PRESENCESYNC_LOG_LEVEL", "info").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def _auto_configure() -> None:
    """Fill in MQTT broker credentials from HA's Supervisor APIs."""
    mqtt_info = await supervisor.discover_mqtt()
    if mqtt_info:
        log.info("auto-discovered MQTT: %s:%s", mqtt_info.host, mqtt_info.port)
        def m(x):
            x.mqtt.host = mqtt_info.host
            x.mqtt.port = mqtt_info.port
            x.mqtt.username = mqtt_info.username
            x.mqtt.password = mqtt_info.password
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

# CORS for local testing (HA ingress handles auth in production)
try:
    from fastapi.middleware.cors import CORSMiddleware
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
except ImportError:
    pass


# ─── Static UI ────────────────────────────────────────────────────────────────

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index():
    """Serve the ingress setup wizard UI."""
    return FileResponse(_STATIC_DIR / "index.html", media_type="text/html")


# ─── Status ──────────────────────────────────────────────────────────────────

@app.get("/api/status")
async def status():
    """Lightweight status for debugging — replaces the old dashboard."""
    coord = get_coord()
    mgr = anisette_manager.get()
    return {
        "version": __import__("presencesync").__version__,
        "findmy_login_state": str(coord.apple.last_login_state),
        "icloud_login_state": coord.icloud.login_state,
        "mqtt_connected": coord.mqtt.connected,
        "airtags_owned": len(coord.apple.accessories),
        "airtags_shared": len(coord.apple.shared_accessories),
        "last_poll_unix": coord.last_run_unix,
        "anisette_running": mgr.running,
        "extractor_available": _extractor_mod.get().available,
        "addon_config": state.get_addon_config().__dict__,
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

    # If iCloud login succeeded, dismiss any auth failure notification and restart loops
    if icloud_state == "logged_in":
        coord._icloud_consecutive_failures = 0
        await supervisor.dismiss_notification("presencesync_reauth_required")
        if coord._stop_event.is_set():
            coord._stop_event.clear()
            coord._poll_task = coord._create_loop_task(coord._poll_loop(), "poll")
            coord._refresh_task = coord._create_loop_task(coord._refresh_loop(), "refresh")
            coord._item_task = coord._create_loop_task(coord._item_loop(), "items")
            log.info("Polling loops restarted after successful re-authentication")

    return {"findmy_state": findmy_state, "icloud_state": icloud_state}


@app.post("/api/apple/2fa/submit")
async def apple_submit_2fa(body: dict):
    """Submit 2FA code to both backends.
    
    Submits to pyicloud first (simple validation), then findmy.py.
    Apple may consume the code on first use, so order matters.
    If one backend fails, the other may still succeed.
    """
    code = body.get("code") or ""
    if not code:
        raise HTTPException(400, "code required")
    coord = get_coord()

    # pyicloud first (simpler validation, less likely to consume the code)
    icloud_state = coord.icloud.login_state
    if icloud_state == "needs_2fa":
        try:
            icloud_state = await asyncio.get_event_loop().run_in_executor(
                None, coord.icloud.submit_2fa, code
            )
        except Exception as e:
            icloud_state = f"ERROR: {type(e).__name__}: {e}"

    # findmy.py
    findmy_state = str(coord.apple.last_login_state)
    if "REQUIRE_2FA" in findmy_state:
        try:
            findmy_state = str(await coord.apple.submit_2fa(code))
        except Exception as e:
            findmy_state = f"ERROR: {type(e).__name__}: {e}"

    # Reload keys if findmy is now logged in
    if "LOGGED_IN" in findmy_state:
        try:
            coord.apple.load_keys_dir()
        except Exception:
            pass

    return {"findmy_state": findmy_state, "icloud_state": icloud_state}


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


# ─── Reset ────────────────────────────────────────────────────────────────────

@app.post("/api/reset")
async def reset():
    """Clear Apple sessions + bundle. Keeps MQTT config."""
    state.clear_apple_state()
    await state.update(lambda s: (
        setattr(s.apple, "username", ""),
        setattr(s.apple, "password", ""),
        setattr(s, "bundle_uploaded", False),
    ))
    coord = get_coord()
    coord.icloud.reset()
    coord.apple.account = None
    coord.apple.accessories = []
    coord.apple.shared_accessories = []
    coord.apple.beaconstore_key = None
    return {"ok": True}


# ─── Key Extraction ───────────────────────────────────────────────────────────

@app.get("/api/extract/status")
async def extract_status():
    """Get current extraction status."""
    ext = _extractor_mod.get()
    s = ext.status
    return {
        "phase": s.phase,
        "message": s.message,
        "bottles": s.bottles,
        "extracted_count": s.extracted_count,
        "error": s.error,
        "available": ext.available,
    }


@app.post("/api/extract/start")
async def extract_start(body: dict):
    """Start key extraction. Requires apple_id."""
    apple_id = body.get("apple_id") or state.get().apple.username
    if not apple_id:
        raise HTTPException(400, "apple_id required")
    mgr = anisette_manager.get()
    if not await mgr.ensure_running():
        raise HTTPException(503, "anisette server not available")
    ext = _extractor_mod.get()
    result = await ext.start_extraction(apple_id, mgr.url)
    return {"phase": result.phase, "message": result.message}


@app.post("/api/extract/password")
async def extract_password(body: dict):
    """Submit password for extraction."""
    password = body.get("password") or ""
    if not password:
        raise HTTPException(400, "password required")
    ext = _extractor_mod.get()
    result = await ext.submit_password(password)
    return {"phase": result.phase, "message": result.message}


@app.post("/api/extract/2fa")
async def extract_2fa(body: dict):
    """Submit 2FA code for extraction."""
    code = body.get("code") or ""
    if not code:
        raise HTTPException(400, "code required")
    ext = _extractor_mod.get()
    result = await ext.submit_2fa(code)
    return {
        "phase": result.phase,
        "message": result.message,
        "bottles": result.bottles,
    }


@app.post("/api/extract/bottle")
async def extract_bottle(body: dict):
    """Select escrow bottle by index."""
    index = body.get("index")
    if index is None:
        raise HTTPException(400, "index required")
    ext = _extractor_mod.get()
    result = await ext.submit_bottle_choice(int(index))
    return {"phase": result.phase, "message": result.message}


@app.post("/api/extract/passcode")
async def extract_passcode(body: dict):
    """Submit device passcode to unlock escrow bottle."""
    passcode = body.get("passcode") or ""
    if not passcode:
        raise HTTPException(400, "passcode required")
    ext = _extractor_mod.get()
    result = await ext.submit_passcode(passcode)
    if result.phase == "done":
        # Reload keys and trigger initial data cycle
        coord = get_coord()
        coord.apple.load_keys_dir()
        coord._reload_cloudkit_mapping()
        asyncio.create_task(coord._initial_data_fetch())
        # Dismiss the repair alert now that keys are loaded
        await supervisor.dismiss_repair("extraction_needed")
    return {
        "phase": result.phase,
        "message": result.message,
        "extracted_count": result.extracted_count,
        "error": result.error,
    }


@app.get("/api/extract/keys")
async def list_keys():
    """List extracted key files."""
    ext = _extractor_mod.get()
    keys = ext.get_extracted_keys()
    return {"keys": [{"name": k.name, "size": k.stat().st_size} for k in keys]}


