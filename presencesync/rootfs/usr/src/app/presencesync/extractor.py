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
        self._output_buffer: str = ""
        self._reader_task: asyncio.Task | None = None
        self._last_bottle_index: int | None = None
        self._last_apple_id: str = ""
        self._last_server_url: str = ""

    @property
    def status(self) -> ExtractionStatus:
        return self._status

    @property
    def available(self) -> bool:
        return EXPORT_BINARY.exists()

    async def _cleanup_process(self) -> None:
        """Kill subprocess if still running, drain reader, then finalize."""
        if self._process and self._process.returncode is None:
            try:
                self._process.kill()
                await self._process.wait()
            except ProcessLookupError:
                pass
        # Drain any remaining output from the reader task
        if self._reader_task and not self._reader_task.done():
            try:
                await asyncio.wait_for(self._reader_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._reader_task.cancel()
        self._finalize()

    async def start_extraction(
        self,
        apple_id: str,
        server_url: str,
    ) -> ExtractionStatus:
        """Start the export-findmy process. Waits for the password prompt before returning."""
        if self._process is not None and self._process.returncode is None:
            return ExtractionStatus(phase="error", error="extraction already in progress")

        if not EXPORT_BINARY.exists():
            return ExtractionStatus(phase="error", error="export-findmy binary not found")

        self._last_apple_id = apple_id
        self._last_server_url = server_url

        KEYS_DIR.mkdir(parents=True, exist_ok=True)

        args = [
            str(EXPORT_BINARY),
            "--output-dir", str(KEYS_DIR),
            "--apple-id", apple_id,
            "--anisette-url", server_url,
        ]

        env = os.environ.copy()

        self._output_lines = []
        self._output_buffer = ""
        self._status = ExtractionStatus(phase="started", message="Starting key extraction...")

        try:
            self._process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=str(state.DATA_DIR),
            )
        except Exception as e:
            self._status = ExtractionStatus(phase="error", error=str(e))
            return self._status

        # Start chunk-based output reader
        self._reader_task = asyncio.create_task(self._read_output())

        # Wait for the binary to print "Password:" before declaring ready
        ready = await self._wait_for_pattern(
            r"[Pp]assword:", timeout=15,
            waiting_msg="Waiting for extraction process to start..."
        )
        if not ready:
            await self._cleanup_process()
            if self._status.phase != "error":
                self._status.phase = "error"
                self._status.error = "Binary did not prompt for password"
                log.warning("export-findmy did not prompt for password within timeout. Output: %s",
                            self._output_buffer[:500])
            return self._status

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

        # Wait for 2FA prompt, bottle list, or process exit (auth failure)
        # Binary does: [1/7] connect anisette → [2/7] login → "2FA code:" or bottles
        found = await self._wait_for_pattern(
            r"2[Ff][Aa] code:|verification code|\[\d+\]",
            timeout=45,
            extended_timeout=45,
            waiting_msg="Logging in to Apple..."
        )
        if not found:
            await self._cleanup_process()
            if self._status.phase != "error":
                self._status.phase = "error"
                self._status.error = "Login timed out"
                log.warning("export-findmy password submission timed out. Output: %s",
                            self._output_buffer[:1000])
        else:
            # Determine what we matched
            if self._detect_2fa_prompt():
                pass  # phase set by _detect_2fa_prompt
            else:
                self._parse_bottles()
                if self._status.phase != "awaiting_bottle":
                    # Process may have exited during parse
                    if self._process and self._process.returncode is not None:
                        self._finalize()
        return self._status

    async def submit_2fa(self, code: str) -> ExtractionStatus:
        """Send the 2FA code to the subprocess."""
        if not self._process or self._process.returncode is not None:
            return ExtractionStatus(phase="error", error="no active extraction")
        assert self._process.stdin is not None
        self._process.stdin.write((code + "\n").encode())
        await self._process.stdin.drain()
        # Wait for bottle list or process exit
        found = await self._wait_for_pattern(
            r"\[\d+\]",
            timeout=30,
            waiting_msg="Verifying 2FA code..."
        )
        if not found:
            await self._cleanup_process()
            if self._status.phase != "error":
                self._status.phase = "error"
                self._status.error = "2FA verification timed out"
        else:
            self._parse_bottles()
        return self._status

    async def _wait_for_pattern(
        self,
        pattern: str,
        timeout: float = 30,
        extended_timeout: float = 0,
        waiting_msg: str = "Processing..."
    ) -> bool:
        """Wait for output matching pattern, process exit, or timeout.
        
        Returns True if pattern was found. Sets status on timeout/exit.
        Uses status-based polling with timeout as escape valve.
        If extended_timeout > 0, logs a warning at first timeout and waits longer.
        """
        import time
        regex = re.compile(pattern)
        start = time.monotonic()
        first_deadline = start + timeout
        final_deadline = start + timeout + extended_timeout
        warned = False

        self._status.message = waiting_msg

        while time.monotonic() < final_deadline:
            await asyncio.sleep(0.3)

            # Check for pattern in full buffer (catches prompts without newlines)
            if regex.search(self._output_buffer):
                log.debug("Pattern %r matched after %.1fs", pattern, time.monotonic() - start)
                return True

            # Check if process exited
            if self._process and self._process.returncode is not None:
                log.debug("Process exited (rc=%d) while waiting for pattern %r",
                          self._process.returncode, pattern)
                return False

            # Warn at first deadline if extended timeout is active
            if not warned and extended_timeout > 0 and time.monotonic() >= first_deadline:
                warned = True
                log.warning("Still waiting for pattern %r after %.0fs (extended wait active). "
                            "Output so far: %s", pattern, timeout, self._output_buffer[:300])
                self._status.message = "Taking longer than expected..."

            # Periodic debug logging
            elapsed = time.monotonic() - start
            if int(elapsed) % 10 == 0 and int(elapsed) > 0:
                log.debug("Waiting for pattern %r (%.0fs elapsed, %d bytes buffered)",
                          pattern, elapsed, len(self._output_buffer))

        log.warning("Timed out waiting for pattern %r after %.0fs. Buffer: %s",
                    pattern, time.monotonic() - start, self._output_buffer[:500])
        return False

    async def submit_bottle_choice(self, index: int) -> ExtractionStatus:
        """Select which escrow bottle to use."""
        if not self._process or self._process.returncode is not None:
            return ExtractionStatus(phase="error", error="no active extraction")
        assert self._process.stdin is not None
        self._last_bottle_index = index
        self._process.stdin.write((str(index) + "\n").encode())
        await self._process.stdin.drain()
        # Wait for passcode prompt ("Enter the passcode of that device:")
        found = await self._wait_for_pattern(
            r"[Pp]asscode",
            timeout=10,
            waiting_msg="Selecting device..."
        )
        if found:
            self._status.phase = "awaiting_passcode"
            self._status.message = "Enter the screen lock passcode for the selected device"
        else:
            await self._cleanup_process()
            if self._status.phase != "error":
                self._status.phase = "error"
                self._status.error = "Did not receive passcode prompt"
        return self._status

    async def submit_passcode(self, passcode: str) -> ExtractionStatus:
        """Send the device passcode to unlock the escrow bottle."""
        # Retry case: process died from wrong passcode, restart transparently
        if not self._process or self._process.returncode is not None:
            return await self._retry_with_passcode(passcode)
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
        # Drain reader before inspecting output
        if self._reader_task and not self._reader_task.done():
            try:
                await asyncio.wait_for(self._reader_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        self._finalize()
        return self._status

    async def _retry_with_passcode(self, passcode: str) -> ExtractionStatus:
        """Restart extraction and auto-advance to submit a new passcode."""
        from . import state as state_mod
        apple_id = self._last_apple_id
        server_url = self._last_server_url
        bottle_index = self._last_bottle_index

        if not apple_id or bottle_index is None:
            return ExtractionStatus(phase="error", error="cannot retry — missing context")

        # Restart extraction
        result = await self.start_extraction(apple_id, server_url)
        if result.phase == "error":
            return result

        # Auto-submit cached password
        cached_pw = state_mod.get().apple.password
        if cached_pw and result.phase == "awaiting_password":
            result = await self.submit_password(cached_pw)
            if result.phase == "error":
                return result

        # Skip 2FA if cached session advanced past it
        if result.phase == "awaiting_2fa":
            # Can't auto-advance past 2FA — ask user to start over
            return ExtractionStatus(phase="error", error="session expired, please start extraction again")

        # Auto-submit bottle
        if result.phase == "awaiting_bottle":
            result = await self.submit_bottle_choice(bottle_index)
            if result.phase == "error":
                return result

        # Now submit the passcode
        if result.phase == "awaiting_passcode":
            self._process.stdin.write((passcode + "\n").encode())
            await self._process.stdin.drain()
            self._status.phase = "running"
            self._status.message = "Extracting keys..."
            try:
                await asyncio.wait_for(self._process.wait(), timeout=120)
            except asyncio.TimeoutError:
                pass
            if self._reader_task and not self._reader_task.done():
                try:
                    await asyncio.wait_for(self._reader_task, timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            self._finalize()

        return self._status

    def _detect_2fa_prompt(self) -> bool:
        """Check if binary is asking for a 2FA code (searches raw buffer for no-newline prompts)."""
        lower = self._output_buffer.lower()
        if "2fa code:" in lower or "verification code" in lower or "two-factor" in lower:
            self._status.phase = "awaiting_2fa"
            self._status.message = "Enter your 2FA code"
            return True
        return False

    def _parse_bottles(self):
        """Parse bottle list from output buffer."""
        bottles = []
        # Binary outputs: [N] SERIAL (Device Name)
        bottle_re = re.compile(r"\[(\d+)\]\s+(\S+)\s+\((.+)\)")
        for line in self._output_buffer.splitlines():
            m = bottle_re.match(line.strip())
            if m:
                idx, serial, name = int(m.group(1)), m.group(2), m.group(3)
                bottles.append({"index": idx, "name": f"{name} ({serial})"})

        # Filter out export-findmy's own escrow bottle
        bottles = [b for b in bottles if "F2LZN0FAKE00" not in b.get("name", "")]

        if bottles:
            self._status.phase = "awaiting_bottle"
            self._status.message = "Select escrow bottle"
            self._status.bottles = bottles

    def _finalize(self):
        """Check results after process completes."""
        if self._process and self._process.returncode == 0:
            self._status = ExtractionStatus(
                phase="done",
                message="Loading extracted keys...",
            )
        else:
            # Check buffer for known error patterns
            tail = self._output_buffer[-2000:] if self._output_buffer else ""
            if "Authentication failed" in tail or "EscrowError" in tail:
                self._status = ExtractionStatus(
                    phase="awaiting_passcode",
                    message="Wrong passcode — try again",
                )
            else:
                self._status = ExtractionStatus(phase="error", error=tail[-500:] or "unknown error")

    async def _read_output(self):
        """Read subprocess output as chunks (not lines) to catch prompts without newlines."""
        if self._process is None or self._process.stdout is None:
            return
        try:
            while True:
                chunk = await self._process.stdout.read(4096)
                if not chunk:
                    break
                text = chunk.decode(errors="replace")
                self._output_buffer += text
                # Also maintain line list for structured parsing
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped:
                        self._output_lines.append(stripped)
                        log.debug("[export-findmy] %s", stripped)
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
