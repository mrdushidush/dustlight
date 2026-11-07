"""Keeps long GPU batches out of the author's way (PLAN.md P7a.1).

The batch runs on the PC he uses every day, so it adapts to what the PC is
doing. Modes:

  full    the PC is locked (LogonUI.exe runs: he is away) -> as fast as it goes,
          even if the game runs: then it is a scripted test run (tests open
          the game only while the PC is locked), not the author playing
  shared  he is at the PC -> after each unit of GPU work, sleep so the batch
          uses about SHARE of the GPU
  paused  he is at the PC and the game runs (fallout2-ce.exe), or the mode
          file says so -> wait. Perf benchmarks set the mode file to `pause`.

`<data_root>/batch_mode.txt` overrides the automatic choice while it exists:
one word, `auto` (default), `full`, `shared` or `pause`. It is read every few
seconds, so a running batch can be paused or let loose without a restart.

The batch process also lowers its own CPU priority (below normal), and child
processes it starts (texconv) inherit that, so the desktop stays responsive,
and caps its VRAM at VRAM_CAP_GB.

`locked_only` (fo2hd retry-tiled): runs only while the PC is locked, with the
VRAM cap raised to RETRY_VRAM_CAP_GB, and hands PyTorch's cached VRAM back
whenever it pauses, so the card is free again when the author returns. The author asked
for this on 2026-11-02: images that didn't fit the 8 GB cap get "all 15 GB we
have available when I'm not using the PC".
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from pathlib import Path

SHARE = 0.5
CHECK_SECONDS = 5.0
VRAM_CAP_GB = 8.0  # + CUDA context ~1 GB: under the 10 GB the author allowed (2026-11-02)
RETRY_VRAM_CAP_GB = 14.0  # + CUDA context ~1 GB: the 15 GB the author allowed while the PC is locked
GAME_PROCESSES = {"fallout2-ce.exe", "fallout2.exe", "fallout2-ce-vanilla.exe", "dustlight.exe"}  # dustlight.exe: the release build (R4)
LOCK_PROCESS = "logonui.exe"

BELOW_NORMAL_PRIORITY_CLASS = 0x4000
IDLE_PRIORITY_CLASS = 0x40  # CPU-side model runs while the author works (fonts)
TH32CS_SNAPPROCESS = 0x2


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def process_names() -> set[str]:
    """Lower-case exe names of all running processes (Toolhelp snapshot)."""
    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, wt.HANDLE(-1).value):
        return set()
    names = set()
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(_ProcessEntry)
    try:
        ok = k32.Process32FirstW(wt.HANDLE(snap), ctypes.byref(entry))
        while ok:
            names.add(entry.szExeFile.lower())
            ok = k32.Process32NextW(wt.HANDLE(snap), ctypes.byref(entry))
    finally:
        k32.CloseHandle(wt.HANDLE(snap))
    return names


def lower_priority(priority_class: int = BELOW_NORMAL_PRIORITY_CLASS) -> None:
    k32 = ctypes.windll.kernel32
    # Without the types ctypes passes the pseudo-handle (-1) as a 32-bit int,
    # which isn't -1 as a 64-bit HANDLE: the call failed and the batch ran at
    # normal priority until this was fixed (2026-11-02).
    k32.GetCurrentProcess.restype = wt.HANDLE
    k32.SetPriorityClass.argtypes = [wt.HANDLE, wt.DWORD]
    k32.SetPriorityClass.restype = wt.BOOL
    if not k32.SetPriorityClass(k32.GetCurrentProcess(), priority_class):
        print(f"    governor: can't lower the CPU priority (error {ctypes.get_last_error() or k32.GetLastError()})", flush=True)


def cap_vram(gb: float = VRAM_CAP_GB) -> None:
    """Caps PyTorch's allocator. Without it, its cache keeps a block per batch
    shape and grew to 11+ GB of the 16 GB card in a few minutes, next to
    the author's browser, Zoom and Remote Desktop. With the cap it reuses and frees
    cached blocks instead; a batch that still doesn't fit is split
    (Upscaler._run)."""
    import torch

    if torch.cuda.is_available():
        total = torch.cuda.get_device_properties(0).total_memory
        torch.cuda.set_per_process_memory_fraction(min(1.0, gb * 2**30 / total))


def release_vram() -> None:
    """Hands PyTorch's cached (allocated but unused) VRAM back to the driver."""
    import torch

    if torch.cuda.is_available():
        torch.cuda.empty_cache()


class Governor:
    def __init__(self, data_root: Path, report=print, share: float = SHARE, locked_only: bool = False):
        self.mode_file = data_root / "batch_mode.txt"
        self.report = report
        self.share = share
        self.locked_only = locked_only
        self._mode = None
        self._checked = 0.0
        self.seconds = {"full": 0.0, "shared": 0.0, "paused": 0.0}
        self._last = time.monotonic()

    def _override(self) -> str:
        try:
            word = self.mode_file.read_text(encoding="utf-8").strip().lower()
        except OSError:
            return "auto"
        return word if word in ("auto", "full", "shared", "pause") else "auto"

    def mode(self) -> str:
        now = time.monotonic()
        if self._mode is None or now - self._checked >= CHECK_SECONDS:
            override = self._override()
            if override == "pause":
                mode = "paused"
            elif override in ("full", "shared"):
                mode = override
            else:
                names = process_names()
                if LOCK_PROCESS in names:
                    mode = "full"
                elif names & GAME_PROCESSES or self.locked_only:
                    mode = "paused"
                else:
                    mode = "shared"
            if mode != self._mode:
                if mode == "paused" and self.locked_only:
                    release_vram()
                self.report(f"    governor: {self._mode or 'start'} -> {mode}" + ("" if override == "auto" else f" ({self.mode_file.name})"))
            self._mode, self._checked = mode, now
        return self._mode

    def _account(self, mode: str) -> None:
        now = time.monotonic()
        self.seconds[mode] += now - self._last
        self._last = now

    def wait(self) -> None:
        """Call before a unit of GPU work: returns once the batch may run."""
        while self.mode() == "paused":
            time.sleep(CHECK_SECONDS)
            self._account("paused")

    def after(self, busy_seconds: float) -> None:
        """Call after a unit of GPU work that took `busy_seconds`."""
        mode = self.mode()
        if mode == "shared" and busy_seconds > 0:
            time.sleep(busy_seconds * (1.0 - self.share) / self.share)
        self._account(mode if mode != "paused" else "shared")
