# ABOUTME: Runs the export-findmy helper to extract Apple Find My accessory keys.
# ABOUTME: Tracks interactive extraction prompts and stores exported plist files under /data.
"""Python wrapper around the embedded export-findmy Rust binary.

Drives the interactive export process via subprocess, handling:
- Device identity persistence (reuses same fake device across runs)
- Interactive prompts (password, 2FA, bottle selection, passcode)
- Output plist parsing
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import state

log = logging.getLogger(__name__)

EXPORT_BINARY = Path("/usr/local/bin/export-findmy")
KEYS_DIR = state.DATA_DIR / "keys"


@dataclass
class ExtractionStatus:
    """Current state of an extraction run."""
    phase: str = "idle"  # idle, started, awaiting_password, awaiting_2fa, awaiting_bottle, awaiting_passcode, running, done, error
    message: str = ""
    bottles: list[dict] = field(default_factory=list)
    extracted_count: int = 0
    error: str = ""


class Extractor:
    """Drives export-findmy as a subprocess with interactive I/O."""

    def __init__(self):
        self._process: asyncio.subprocess.Process | None = None
        self._status = ExtractionStatus()
        self._output_lines: list[str] = []
        self._reader_task: asyncio.Task | None = None

    @property
    def status(self) -> ExtractionStatus:
        return self._status

    @property
    def available(self) -> bool:
        return EXPORT_BINARY.exists()

    async def start_extraction(
        self,
        apple_id: str,
        server_url: str,
    ) -> ExtractionStatus:
        """Start the export-findmy process. Prompts for password next."""
        if self._process is not None and self._process.returncode is None:
            return ExtractionStatus(phase="error", error="extraction already in progress")

        if not EXPORT_BINARY.exists():
            return ExtractionStatus(phase="error", error="export-findmy binary not found")

        KEYS_DIR.mkdir(parents=True, exist_ok=True)

        args = [
            str(EXPORT_BINARY),
            "--output-dir", str(KEYS_DIR),
            "--apple-id", apple_id,
            "--anisette-url", server_url,
        ]

        env = os.environ.copy()

        self._output_lines = []
        self._status = ExtractionStatus(phase="started", message="Starting key extraction...")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=str(state.DATA_DIR),  # binary reads/writes device_identity.json from cwd
            )
        except Exception as e:
            self._status = ExtractionStatus(phase="error", error=str(e))
            return self._status

        # Start reading output
        self._reader_task = asyncio.create_task(self._read_output())
        self._status.phase = "awaiting_password"
        self._status.message = "Enter your Apple ID password"
        return self._status

    async def submit_password(self, password: str) -> ExtractionStatus:
        """Send the Apple ID password to the subprocess."""
        if not self._process or self._process.returncode is not None:
            return ExtractionStatus(phase="error", error="no active extraction")
        assert self._process.stdin is not None
        self._process.stdin.write((password + "\n").encode())
        await self._process.stdin.drain()
        # Wait for output — binary may skip 2FA if session is cached
        await self._wait_for_next_prompt(timeout=10)
        return self._status

    async def submit_2fa(self, code: str) -> ExtractionStatus:
        """Send the 2FA code to the subprocess."""
        if not self._process or self._process.returncode is not None:
            return ExtractionStatus(phase="error", error="no active extraction")
        assert self._process.stdin is not None
        self._process.stdin.write((code + "\n").encode())
        await self._process.stdin.drain()
        # Wait for bottle list or completion
        await self._wait_for_next_prompt(timeout=15)
        return self._status

    async def _wait_for_next_prompt(self, timeout: float = 10):
        """Poll output to detect what the binary is waiting for next."""
        deadline = asyncio.get_event_loop().time() + timeout
        prev_lines = len(self._output_lines)
        self._status.phase = "running"
        self._status.message = "Processing..."
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            # Check if process exited
            if self._process and self._process.returncode is not None:
                self._finalize()
                return
            # Check for new output indicating a prompt
            if len(self._output_lines) > prev_lines:
                prev_lines = len(self._output_lines)
                if self._detect_2fa_prompt():
                    return
                self._parse_bottles()
                if self._status.phase == "awaiting_bottle":
                    return

    async def submit_bottle_choice(self, index: int) -> ExtractionStatus:
        """Select which escrow bottle to use."""
        if not self._process or self._process.returncode is not None:
            return ExtractionStatus(phase="error", error="no active extraction")
        assert self._process.stdin is not None
        self._process.stdin.write((str(index) + "\n").encode())
        await self._process.stdin.drain()
        self._status.phase = "awaiting_passcode"
        self._status.message = "Enter the device screen lock passcode for the selected bottle"
        return self._status

    async def submit_passcode(self, passcode: str) -> ExtractionStatus:
        """Send the device passcode to unlock the escrow bottle."""
        if not self._process or self._process.returncode is not None:
            return ExtractionStatus(phase="error", error="no active extraction")
        assert self._process.stdin is not None
        self._process.stdin.write((passcode + "\n").encode())
        await self._process.stdin.drain()
        self._status.phase = "running"
        self._status.message = "Extracting keys..."
        # Wait for process to finish
        try:
            await asyncio.wait_for(self._process.wait(), timeout=120)
        except asyncio.TimeoutError:
            pass
        self._finalize()
        return self._status

    def _detect_2fa_prompt(self) -> bool:
        """Check if binary is asking for a 2FA code."""
        for line in self._output_lines:
            lower = line.lower()
            if "2fa" in lower or "verification code" in lower or "two-factor" in lower:
                self._status.phase = "awaiting_2fa"
                self._status.message = "Enter your 2FA code"
                return True
        return False

    def _parse_bottles(self):
        """Parse bottle list from output."""
        bottles = []
        # Binary outputs: [N] SERIAL (Device Name)
        bottle_re = re.compile(r"\[(\d+)\]\s+(.+)")
        for line in self._output_lines:
            m = bottle_re.match(line.strip())
            if m:
                bottles.append({"index": int(m.group(1)), "name": m.group(2)})

        # Filter out pyicloud's fake escrow bottles (serial contains "FAKE")
        bottles = [b for b in bottles if "FAKE" not in b.get("name", "")]

        if bottles:
            self._status.phase = "awaiting_bottle"
            self._status.message = "Select escrow bottle"
            self._status.bottles = bottles

    def _finalize(self):
        """Check results after process completes."""
        if self._process and self._process.returncode == 0:
            # Count extracted plists
            plists = list(KEYS_DIR.glob("*.plist"))
            self._status = ExtractionStatus(
                phase="done",
                message=f"Extracted {len(plists)} accessory key(s)",
                extracted_count=len(plists),
            )
        else:
            error = "\n".join(self._output_lines[-20:]) if self._output_lines else "unknown error"
            self._status = ExtractionStatus(phase="error", error=error)

    async def _read_output(self):
        """Read subprocess output line by line."""
        if self._process is None or self._process.stdout is None:
            return
        try:
            async for line in self._process.stdout:
                text = line.decode(errors="replace").rstrip()
                self._output_lines.append(text)
                log.debug("[export-findmy] %s", text)
        except Exception:
            pass

    def get_extracted_keys(self) -> list[Path]:
        """List all extracted plist files."""
        if not KEYS_DIR.exists():
            return []
        return sorted(KEYS_DIR.glob("*.plist"))

    def clear_keys(self):
        """Remove all extracted keys."""
        if KEYS_DIR.exists():
            shutil.rmtree(KEYS_DIR)
        KEYS_DIR.mkdir(parents=True, exist_ok=True)


# Module singleton
_extractor: Extractor | None = None


def get() -> Extractor:
    global _extractor
    if _extractor is None:
        _extractor = Extractor()
    return _extractor
