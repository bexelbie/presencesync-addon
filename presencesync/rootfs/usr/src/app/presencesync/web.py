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

# Suppress noisy uvicorn access logs for GET requests (status polling)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


async def _auto_configure() -> None:
    """Fill in MQTT broker credentials from HA's Supervisor APIs or env vars."""
    mqtt_info = await supervisor.discover_mqtt()
    if mqtt_info:
        log.info("auto-discovered MQTT: %s:%s", mqtt_info.host, mqtt_info.port)
        def m(x):
            x.mqtt.host = mqtt_info.host
            x.mqtt.port = mqtt_info.port
            x.mqtt.username = mqtt_info.username
            x.mqtt.password = mqtt_info.password
        await state.update(m)
    elif os.environ.get("MQTT_HOST"):
        host = os.environ["MQTT_HOST"]
        port = int(os.environ.get("MQTT_PORT", "1883"))
        username = os.environ.get("MQTT_USERNAME", "")
        password = os.environ.get("MQTT_PASSWORD", "")
        log.info("MQTT from env vars: %s:%s", host, port)
        def m(x):
            x.mqtt.host = host
            x.mqtt.port = port
            x.mqtt.username = username
            x.mqtt.password = password
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
        "airtags_tracked": len(coord._prev_item_ids),
        "idevices_tracked": len(coord._prev_idevice_ids),
        "device_fixes": len(coord.last_device_fixes),
        "last_poll_unix": coord.last_poll_unix,
        "last_refresh_unix": coord.last_refresh_unix,
        "last_item_fetch_unix": coord.last_item_fetch_unix,
        "poll_interval": int(state.get_addon_config().poll_interval),
        "refresh_interval": int(state.get_addon_config().refresh_interval),
        "item_poll_interval": int(state.get_addon_config().item_poll_interval),
        "anisette_running": mgr.running,
        "anisette_healthy": await mgr.health_check() if mgr.running else False,
        "extractor_available": _extractor_mod.get().available,
        "apple_username": state.get().apple.username or "",
    }


# ─── Manual triggers ──────────────────────────────────────────────────────────

@app.post("/api/poll-now")
async def poll_now():
    """Trigger an immediate iDevice poll (lightweight, cached positions)."""
    coord = get_coord()
    await coord._do_poll()
    return {"ok": True}


@app.post("/api/refresh-now")
async def refresh_now():
    """Trigger an immediate iDevice refresh (forces location update)."""
    coord = get_coord()
    await coord._do_refresh()
    return {"ok": True}


@app.post("/api/fetch-items-now")
async def fetch_items_now():
    """Trigger an immediate AirTag/item fetch."""
    coord = get_coord()
    await coord._do_fetch_items()
    return {"ok": True}


# ─── Apple Login + 2FA ────────────────────────────────────────────────────────

@app.post("/api/apple/login")
async def apple_login(body: dict):
    """Authenticate to both findmy.py (AirTags) and pyicloud (iDevices).
    
    Optionally accepts a 2FA code to submit in the same request.
    """
    username = body.get("username") or ""
    password = body.get("password") or ""
    code = body.get("code") or ""
    if not username or not password:
        raise HTTPException(400, "username and password required")

    # Gate on anisette — findmy.py needs it for authentication
    mgr = anisette_manager.get()
    if not await mgr.ensure_running():
        raise HTTPException(503, "Authentication service is not ready. Please wait and try again.")

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

    # Auto-submit 2FA if code provided
    if code:
        if icloud_state == "needs_2fa":
            try:
                icloud_state = await asyncio.get_event_loop().run_in_executor(
                    None, coord.icloud.submit_2fa, code
                )
            except Exception as e:
                icloud_state = f"ERROR: {type(e).__name__}: {e}"
        if "REQUIRE_2FA" in findmy_state:
            try:
                findmy_state = str(await coord.apple.submit_2fa(code))
            except Exception as e:
                findmy_state = f"ERROR: {type(e).__name__}: {e}"

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
    """Start key extraction. Auto-submits cached password if available."""
    apple_id = body.get("apple_id") or state.get().apple.username
    if not apple_id:
        raise HTTPException(400, "apple_id required")
    mgr = anisette_manager.get()
    if not await mgr.ensure_running():
        raise HTTPException(503, "anisette server not available")
    ext = _extractor_mod.get()
    log.info("Starting AirTag extraction for %s", apple_id)
    result = await ext.start_extraction(apple_id, mgr.url)
    log.debug("start_extraction returned phase=%s", result.phase)

    # Auto-submit cached password so user skips straight to 2FA
    if result.phase == "awaiting_password":
        cached_pw = state.get().apple.password
        if cached_pw:
            log.debug("Auto-submitting cached password")
            result = await ext.submit_password(cached_pw)
            log.debug("Auto-submit result: phase=%s error=%s", result.phase, result.error)
            if result.phase == "error":
                log.warning("Auto-submit cached password failed: %s", result.error)
                await state.update(lambda s: setattr(s.apple, "password", ""))
                result = await ext.start_extraction(apple_id, mgr.url)
                result.message = f"AirTag login failed — enter your Apple ID password. (Detail: {result.message or 'unknown'})"
        else:
            log.debug("No cached password, prompting user")

    return {"phase": result.phase, "message": result.message, "bottles": result.bottles}


@app.post("/api/extract/password")
async def extract_password(body: dict):
    """Submit password for extraction. Caches it for future use."""
    password = body.get("password") or ""
    if not password:
        raise HTTPException(400, "password required")
    ext = _extractor_mod.get()
    log.debug("Manual password submission for extraction")
    result = await ext.submit_password(password)
    log.debug("Manual submit result: phase=%s error=%s", result.phase, result.error)
    if result.phase == "error":
        log.warning("Manual password submission failed: %s", result.error)
        apple_id = state.get().apple.username
        mgr = anisette_manager.get()
        error_detail = result.error or "unknown"
        result = await ext.start_extraction(apple_id, mgr.url)
        result.message = f"AirTag login failed — try again. (Detail: {error_detail})"
    else:
        log.info("Extraction password accepted, phase=%s", result.phase)
        # Cache the password since Apple ID password is shared across all flows
        await state.update(lambda s: setattr(s.apple, "password", password))
    return {"phase": result.phase, "message": result.message, "bottles": result.bottles}


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
        real_count = len(coord.apple.accessories) + len(coord.apple.shared_accessories)
        coord._reload_cloudkit_mapping()
        asyncio.create_task(coord._initial_data_fetch())
        await supervisor.dismiss_notification("presencesync_extraction_needed")
        # Reset extractor to idle so status doesn't persist stale "done"
        ext._status = ext._status.__class__(phase="idle")
        return {
            "phase": "done",
            "message": f"✓ Extracted {real_count} AirTag key(s). Tracking will begin shortly.",
            "extracted_count": real_count,
        }
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


