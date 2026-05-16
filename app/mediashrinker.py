#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import concurrent.futures
import itertools
import json
import os
import queue
import re
import select
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set

try:
    import pty  # Unix only
    HAVE_PTY = True
except Exception:
    pty = None  # type: ignore[assignment]
    HAVE_PTY = False
from mediashrinker_core import (
    Analysis,
    DEFAULT_PROFILE,
    ENCODING_PROFILES,
    apply_rules,
    content_kind_for_path,
    get_profile,
    pick_transcode_cq,
    pick_transcode_vb_kbps,
    get_completed_paths,
    persist_run_to_db,
)

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".wmv"}
TEXT_EXTS  = {".srt", ".ass", ".ssa", ".vtt"}

DEFAULT_MOVIES_ROOT  = Path("/mnt/lmenas-movies")
DEFAULT_SERIES_ROOT  = Path("/mnt/lmenas-series")
DEFAULT_STAGING_DIR  = Path(os.environ.get("MEDIA_STAGING_DIR", "/staging"))
DEFAULT_REPORT_DIR   = Path(os.environ.get("MEDIA_REPORT_DIR", "/reports"))
DEFAULT_CFG_PATH     = Path.home() / ".mediashrinker.json"

if os.name == "nt":
    DEFAULT_TESSDATA_PREFIX = Path(r"C:\Program Files\Tesseract-OCR\tessdata")
    DEFAULT_PGSRIP_BIN = "pgsrip"
else:
    DEFAULT_TESSDATA_PREFIX = Path("/usr/share/tesseract-ocr/5/tessdata")
    DEFAULT_PGSRIP_BIN = str(Path.home() / ".local/bin/pgsrip")

GROWTH_GUARD_MAX_RATIO = 1.05  # keep original if encoded output exceeds +5%

# -----------------------
# Progress helpers
# -----------------------

RE_PROGRESS = re.compile(
    r"Encoding:\s*task\s*(\d+)\s*of\s*(\d+),\s*([0-9.]+)\s*%.*?(?:\(\s*([0-9.]+)\s*fps.*?)?ETA\s*([0-9]{2}h[0-9]{2}m[0-9]{2}s|[0-9]{2}:[0-9]{2}:[0-9]{2})",
    re.IGNORECASE,
)
PROGRESS_INLINE = True
STOP_REQUESTED = threading.Event()
ACTIVE_PROCS: Set[subprocess.Popen] = set()
ACTIVE_PROCS_LOCK = threading.Lock()
CONSOLE_LOCK = threading.Lock()
PROGRESS_SLOTS = 0
THREAD_CTX = threading.local()
PROGRESS_STATE_LOCK = threading.Lock()
PROGRESS_STATE: Dict[str, Tuple[float, float]] = {}
PARALLEL_FIXED_ROWS = False
RUN_ACTIVE = False
_VT_CHECKED = False
_VT_SUPPORTED = False
_WIN_CHECKED = False
_WIN_SUPPORTED = False
_WIN_HANDLE: Any = None
_WIN_CSBI_TYPE: Any = None
_PROGRESS_RENDER_MODE = "plain"  # plain | vt | winapi
_PROGRESS_BASE_Y: Optional[int] = None
_SLOT_LAST_LEN: Dict[int, int] = {}
LIVE_SLOT_STATUS_LOCK = threading.Lock()
LIVE_SLOT_STATUS: Dict[int, str] = {}

def register_proc(p: subprocess.Popen) -> None:
    with ACTIVE_PROCS_LOCK:
        ACTIVE_PROCS.add(p)

def unregister_proc(p: subprocess.Popen) -> None:
    with ACTIVE_PROCS_LOCK:
        ACTIVE_PROCS.discard(p)

def terminate_active_procs() -> None:
    with ACTIVE_PROCS_LOCK:
        procs = list(ACTIVE_PROCS)
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass

def set_thread_progress_slot(slot: Optional[int]) -> None:
    THREAD_CTX.slot = slot

def get_thread_progress_slot() -> Optional[int]:
    return getattr(THREAD_CTX, "slot", None)

def _stdout_supports_vt() -> bool:
    global _VT_CHECKED, _VT_SUPPORTED
    if _VT_CHECKED:
        return _VT_SUPPORTED
    _VT_CHECKED = True
    if not sys.stdout.isatty():
        _VT_SUPPORTED = False
        return False
    if os.name != "nt":
        _VT_SUPPORTED = True
        return True
    try:
        import ctypes  # Windows-only path

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if handle == 0 or handle == -1:
            _VT_SUPPORTED = False
            return False
        mode = ctypes.c_uint()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) == 0:
            _VT_SUPPORTED = False
            return False
        enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if (mode.value & enable_vt) == 0:
            if kernel32.SetConsoleMode(handle, mode.value | enable_vt) == 0:
                _VT_SUPPORTED = False
                return False
        _VT_SUPPORTED = True
        return True
    except Exception:
        _VT_SUPPORTED = False
        return False

def _init_win_console_api() -> bool:
    global _WIN_CHECKED, _WIN_SUPPORTED, _WIN_HANDLE, _WIN_CSBI_TYPE
    if _WIN_CHECKED:
        return _WIN_SUPPORTED
    _WIN_CHECKED = True
    if os.name != "nt" or not sys.stdout.isatty():
        _WIN_SUPPORTED = False
        return False
    try:
        import ctypes

        class _COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class _SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short), ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

        class _CSBI(ctypes.Structure):
            _fields_ = [
                ("dwSize", _COORD),
                ("dwCursorPosition", _COORD),
                ("wAttributes", ctypes.c_ushort),
                ("srWindow", _SMALL_RECT),
                ("dwMaximumWindowSize", _COORD),
            ]

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if handle == 0 or handle == -1:
            _WIN_SUPPORTED = False
            return False
        csbi = _CSBI()
        if kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(csbi)) == 0:
            _WIN_SUPPORTED = False
            return False

        _WIN_HANDLE = handle
        _WIN_CSBI_TYPE = _CSBI
        _WIN_SUPPORTED = True
        return True
    except Exception:
        _WIN_SUPPORTED = False
        return False

def _win_get_cursor_y() -> Optional[int]:
    if not _init_win_console_api():
        return None
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        csbi = _WIN_CSBI_TYPE()
        if kernel32.GetConsoleScreenBufferInfo(_WIN_HANDLE, ctypes.byref(csbi)) == 0:
            return None
        return int(csbi.dwCursorPosition.Y)
    except Exception:
        return None

def _win_set_cursor(x: int, y: int) -> bool:
    if not _init_win_console_api():
        return False
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        class _COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        return bool(kernel32.SetConsoleCursorPosition(_WIN_HANDLE, _COORD(int(x), int(y))))
    except Exception:
        return False

def init_progress_slots(n: int) -> None:
    global PROGRESS_SLOTS, PARALLEL_FIXED_ROWS, _PROGRESS_RENDER_MODE, _PROGRESS_BASE_Y
    PROGRESS_SLOTS = max(0, int(n))
    _SLOT_LAST_LEN.clear()
    with LIVE_SLOT_STATUS_LOCK:
        LIVE_SLOT_STATUS.clear()
    _PROGRESS_BASE_Y = None
    _PROGRESS_RENDER_MODE = "plain"
    if PROGRESS_SLOTS > 1:
        if _stdout_supports_vt():
            _PROGRESS_RENDER_MODE = "vt"
        elif _init_win_console_api():
            _PROGRESS_RENDER_MODE = "winapi"
    PARALLEL_FIXED_ROWS = (PROGRESS_SLOTS > 1) and (_PROGRESS_RENDER_MODE in ("vt", "winapi"))
    if PROGRESS_SLOTS > 1:
        with CONSOLE_LOCK:
            if PARALLEL_FIXED_ROWS:
                for i in range(1, PROGRESS_SLOTS + 1):
                    print(f"[J{i}] idle")
                    _SLOT_LAST_LEN[i] = len(f"[J{i}] idle")
                    with LIVE_SLOT_STATUS_LOCK:
                        LIVE_SLOT_STATUS[i] = f"[J{i}] idle"
                if _PROGRESS_RENDER_MODE == "winapi":
                    y = _win_get_cursor_y()
                    if y is None:
                        PARALLEL_FIXED_ROWS = False
                        _PROGRESS_RENDER_MODE = "plain"
                    else:
                        _PROGRESS_BASE_Y = int(y) - PROGRESS_SLOTS
            else:
                print("[INFO] terminal without VT cursor control: multiline progress mode.")

def _update_progress_slot(slot: int, text: str) -> None:
    if PROGRESS_SLOTS <= 1:
        with CONSOLE_LOCK:
            if text:
                print(text, flush=True)
        return
    if not PARALLEL_FIXED_ROWS:
        with CONSOLE_LOCK:
            if text:
                print(text, flush=True)
        return
    slot = max(1, min(PROGRESS_SLOTS, int(slot)))
    line = (text or "").replace("\r", " ").replace("\n", " ").rstrip()
    with LIVE_SLOT_STATUS_LOCK:
        LIVE_SLOT_STATUS[slot] = line
    with CONSOLE_LOCK:
        if _PROGRESS_RENDER_MODE == "winapi":
            if _PROGRESS_BASE_Y is None:
                if line:
                    print(line, flush=True)
                return
            target_y = _PROGRESS_BASE_Y + (slot - 1)
            restore_y = _PROGRESS_BASE_Y + PROGRESS_SLOTS
            if not _win_set_cursor(0, target_y):
                if line:
                    print(line, flush=True)
                return
            prev_len = _SLOT_LAST_LEN.get(slot, 0)
            pad = max(0, prev_len - len(line))
            sys.stdout.write(line + (" " * pad))
            sys.stdout.flush()
            _SLOT_LAST_LEN[slot] = len(line)
            _win_set_cursor(0, restore_y)
            return
        # Save cursor, update the reserved slot row, restore cursor.
        save = "\x1b[s"
        restore = "\x1b[u"
        up = f"\x1b[{PROGRESS_SLOTS}A"
        down_to_slot = f"\x1b[{slot - 1}B" if slot > 1 else ""
        clear_line = "\r\x1b[2K"
        print(save + up + down_to_slot + clear_line + line + restore, end="", flush=True)

def _clear_progress_slot(slot: int) -> None:
    if PROGRESS_SLOTS <= 1:
        return
    _update_progress_slot(slot, f"[J{slot}] idle")

def snapshot_live_slots() -> List[Dict[str, Any]]:
    with LIVE_SLOT_STATUS_LOCK:
        items = sorted(LIVE_SLOT_STATUS.items(), key=lambda kv: kv[0])
    out: List[Dict[str, Any]] = []
    for slot, text in items:
        t = str(text or "")
        is_idle = (" idle" in t.lower()) or (t.strip().lower().endswith("idle"))
        out.append({"slot": int(slot), "text": t, "is_idle": bool(is_idle)})
    return out

def short_label(s: str, max_len: int = 40) -> str:
    x = (s or "").strip()
    if len(x) <= max_len:
        return x
    return x[: max_len - 3] + "..."

def should_emit_progress(prefix: str, pct: float, now: float, *, force: bool = False) -> bool:
    if STOP_REQUESTED.is_set():
        return False
    if PROGRESS_SLOTS <= 1:
        return True
    with PROGRESS_STATE_LOCK:
        last_pct, last_t = PROGRESS_STATE.get(prefix, (-1.0, 0.0))
        if force or pct >= 100.0 or (pct - last_pct) >= 2.0 or (now - last_t) >= 2.0:
            PROGRESS_STATE[prefix] = (pct, now)
            return True
    return False

def eta_to_seconds(s: str) -> Optional[int]:
    s = s.strip()
    m = re.match(r"(\d{2})h(\d{2})m(\d{2})s", s)
    if m:
        h, mi, se = map(int, m.groups())
        return h * 3600 + mi * 60 + se
    m = re.match(r"(\d{2}):(\d{2}):(\d{2})", s)
    if m:
        h, mi, se = map(int, m.groups())
        return h * 3600 + mi * 60 + se
    return None

def fmt_eta(seconds: Optional[int]) -> str:
    if seconds is None:
        return "??:??"
    s = int(seconds)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"

def parse_media_time_to_seconds(s: str) -> Optional[float]:
    x = (s or "").strip()
    if not x or x.upper() == "N/A":
        return None
    m = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", x)
    if not m:
        return None
    h = int(m.group(1))
    mi = int(m.group(2))
    sec = float(m.group(3))
    return float(h * 3600 + mi * 60) + sec

def estimate_encode_eta_seconds(
    *,
    duration_sec: Optional[float],
    out_time_str: str,
    speed_str: str,
) -> Optional[int]:
    if duration_sec is None or duration_sec <= 0:
        return None
    out_s = parse_media_time_to_seconds(out_time_str)
    if out_s is None:
        return None
    try:
        spd = float((speed_str or "").strip().rstrip("xX"))
    except Exception:
        return None
    if spd <= 0:
        return None
    remaining_media = max(0.0, float(duration_sec) - out_s)
    eta = int(round(remaining_media / spd))
    return max(0, eta)

def human_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n)
    for u in units:
        if x < 1024.0 or u == units[-1]:
            return f"{x:.2f} {u}" if u != "B" else f"{int(x)} {u}"
        x /= 1024.0
    return f"{n} B"

def print_bar(prefix: str, pct: float, eta_s: Optional[int], fps: Optional[float]) -> None:
    width = 28
    filled = max(0, min(width, int(width * pct / 100.0)))
    bar = "#" * filled + "-" * (width - filled)
    eta_txt = fmt_eta(eta_s)
    fps_txt = f"{fps:.1f}fps" if fps is not None else ""
    line = f"{prefix} [{bar}] {pct:6.2f}% ETA {eta_txt} {fps_txt}   "
    slot = get_thread_progress_slot()
    if PROGRESS_SLOTS > 1 and slot is not None:
        _update_progress_slot(slot, line)
    elif PROGRESS_INLINE:
        print("\r" + line, end="", flush=True)
    else:
        print(line, flush=True)

def print_copy_bar(prefix: str, pct: float, eta_s: Optional[int], mb_s: Optional[float]) -> None:
    width = 28
    filled = max(0, min(width, int(width * pct / 100.0)))
    bar = "#" * filled + "-" * (width - filled)
    eta_txt = fmt_eta(eta_s)
    spd_txt = f"{mb_s:.1f}MB/s" if mb_s is not None else ""
    line = f"{prefix} [{bar}] {pct:6.2f}% ETA {eta_txt} {spd_txt}   "
    slot = get_thread_progress_slot()
    if PROGRESS_SLOTS > 1 and slot is not None:
        _update_progress_slot(slot, line)
    elif PROGRESS_INLINE:
        print("\r" + line, end="", flush=True)
    else:
        print(line, flush=True)

def print_spinner(prefix: str, spin: str, extracted_bytes: int, mb_s: Optional[float]) -> None:
    spd_txt = f"{mb_s:.1f}MB/s" if mb_s is not None else ""
    line = f"{prefix} [{spin}] {human_bytes(extracted_bytes)} {spd_txt}   "
    slot = get_thread_progress_slot()
    if PROGRESS_SLOTS > 1 and slot is not None:
        _update_progress_slot(slot, line)
    elif PROGRESS_INLINE:
        print("\r" + line, end="", flush=True)
    else:
        print(line, flush=True)

def copy_with_progress(src: Path, dst: Path, prefix: str, chunk_size: int = 16 * 1024 * 1024) -> None:
    total = src.stat().st_size
    copied = 0
    t0 = time.time()
    last = 0.0

    dst.parent.mkdir(parents=True, exist_ok=True)

    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            if STOP_REQUESTED.is_set():
                raise InterruptedError("stop requested")
            buf = fsrc.read(chunk_size)
            if not buf:
                break
            fdst.write(buf)
            copied += len(buf)

            now = time.time()
            if (now - last) > 0.2 or copied == total:
                if STOP_REQUESTED.is_set():
                    raise InterruptedError("stop requested")
                elapsed = max(now - t0, 0.001)
                speed = copied / elapsed
                mb_s = speed / 1024 / 1024
                eta = int((total - copied) / speed) if speed > 0 else None
                pct = (copied / total * 100.0) if total else 0.0
                if should_emit_progress(prefix, pct, now, force=(copied == total)):
                    print_copy_bar(prefix, pct, eta, mb_s)
                last = now

        fdst.flush()
        try:
            os.fsync(fdst.fileno())
        except Exception:
            pass

    try:
        shutil.copystat(src, dst)
    except Exception:
        pass

    if PROGRESS_SLOTS <= 1:
        print()

# -----------------------
# Config (remember last choices)
# -----------------------

@dataclass
class MediaShrinkerConfig:
    library_choice: str = "1"    # 1 movies, 2 series, 3 both
    mode_choice: str = "2"       # 1 plan, 2 run, 3 run+cleanup, 4 cleanup-only
    jobs: int = 1
    vb_kbps: int = 30000
    bitrate_threshold_mbps: float = 55.0
    bitrate_4k_mbps: float = 45.0
    no_multipass: bool = False
    extract_pgs: bool = True
    ocr_engine: str = "pgsrip"   # pgsrip or none
    ocr_target_langs: str = "ita,eng"
    tessdata_prefix: str = str(DEFAULT_TESSDATA_PREFIX)
    pgsrip_bin: str = str(DEFAULT_PGSRIP_BIN)
    add_external_text_subs: bool = True
    encoding_profile: str = DEFAULT_PROFILE

def load_cfg(path: Path) -> MediaShrinkerConfig:
    try:
        if not path.exists():
            return MediaShrinkerConfig()
        data = json.loads(path.read_text(encoding="utf-8"))
        cfg = MediaShrinkerConfig()
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        if cfg.library_choice not in ("1", "2", "3"):
            cfg.library_choice = "1"
        if cfg.mode_choice not in ("1", "2", "3", "4"):
            cfg.mode_choice = "2"
        if not isinstance(cfg.jobs, int) or cfg.jobs < 1:
            cfg.jobs = 1
        if not isinstance(cfg.vb_kbps, int) or cfg.vb_kbps < 1000:
            cfg.vb_kbps = 30000
        if cfg.ocr_engine not in ("pgsrip", "none"):
            cfg.ocr_engine = "pgsrip"
        if not parse_lang_list(str(cfg.ocr_target_langs or "")):
            cfg.ocr_target_langs = "ita,eng"
        return cfg
    except Exception:
        return MediaShrinkerConfig()

def save_cfg(path: Path, cfg: MediaShrinkerConfig) -> None:
    try:
        path.write_text(json.dumps(asdict(cfg), indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def pgsrip_is_available(pgsrip_bin: str) -> bool:
    if not pgsrip_bin:
        return False
    p = Path(pgsrip_bin)
    return p.exists() or (shutil.which(pgsrip_bin) is not None)

def normalize_windows_ocr_values(ocr_engine: str, pgsrip_bin: str, tessdata_prefix: str) -> Tuple[str, str, str]:
    if os.name != "nt":
        return ocr_engine, pgsrip_bin, tessdata_prefix
    pb = (pgsrip_bin or "").strip()
    td = (tessdata_prefix or "").strip()

    # Convert legacy Linux-like values imported from WSL config.
    if ("/.local/bin/pgsrip" in pb) or ("\\.local\\bin\\pgsrip" in pb):
        pb = "pgsrip"
    # If a broken launcher injected pip output around the executable path, recover the executable token.
    pbm = re.search(r"([A-Za-z]:\\[^\"'\r\n]*pgsrip(?:\.exe)?)", pb)
    if pbm:
        pb = pbm.group(1)
    elif "pgsrip" in pb.lower():
        pb = "pgsrip"
    if ("/usr/share/tesseract-ocr" in td) or ("\\usr\\share\\tesseract-ocr" in td):
        td = str(DEFAULT_TESSDATA_PREFIX)

    # If engine is pgsrip but binary is not available on Windows, disable OCR automatically.
    if (ocr_engine == "pgsrip") and (not pgsrip_is_available(pb)):
        ocr_engine = "none"
    return ocr_engine, (pb or "pgsrip"), (td or str(DEFAULT_TESSDATA_PREFIX))

# -----------------------
# System helpers
# -----------------------

def which_or(name: str) -> str:
    found = shutil.which(name)
    return found or name

def run_cmd_capture(cmd: List[str], *, env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    register_proc(p)
    try:
        while True:
            if STOP_REQUESTED.is_set():
                try:
                    p.terminate()
                except Exception:
                    pass
                return 130, "", "terminated by user"
            try:
                out, err = p.communicate(timeout=0.2)
                return int(p.returncode or 0), out or "", err or ""
            except subprocess.TimeoutExpired:
                continue
    finally:
        unregister_proc(p)

# -----------------------
# Media analysis (ffprobe)
# -----------------------

def ffprobe_json(ffprobe: str, path: Path) -> Dict[str, Any]:
    cmd = [
        ffprobe,
        "-hide_banner", "-loglevel", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,bit_rate,side_data_list:"
        "format=format_name,bit_rate,duration",
        str(path),
    ]
    rc, out, err = run_cmd_capture(cmd)
    if rc != 0:
        raise RuntimeError(f"ffprobe failed: {err.strip()}")
    return json.loads(out)

def analyze_one(ffprobe: str, path: Path) -> Analysis:
    st = path.stat()
    data = ffprobe_json(ffprobe, path)

    fmt = data.get("format", {}) or {}
    container = (fmt.get("format_name") or "").split(",")[0] or ""
    fmt_bitrate = fmt.get("bit_rate")
    fmt_duration = fmt.get("duration")
    container_bitrate = int(fmt_bitrate) if fmt_bitrate and str(fmt_bitrate).isdigit() else None
    duration_sec: Optional[float] = None
    try:
        if fmt_duration is not None:
            duration_sec = float(fmt_duration)
    except Exception:
        duration_sec = None

    v_stream = None
    a_streams: List[Dict[str, Any]] = []
    for s in (data.get("streams", []) or []):
        if s.get("codec_type") == "video" and v_stream is None:
            v_stream = s
        elif s.get("codec_type") == "audio":
            a_streams.append(s)

    v_codec = v_stream.get("codec_name") if v_stream else None

    v_bitrate = None
    if v_stream:
        vb = v_stream.get("bit_rate")
        if vb and str(vb).isdigit():
            v_bitrate = int(vb)
    if v_bitrate is None:
        v_bitrate = container_bitrate

    v_width = v_stream.get("width") if v_stream else None
    v_height = v_stream.get("height") if v_stream else None

    dv_profile = None
    dv_el_present = None
    if v_stream:
        for sd in (v_stream.get("side_data_list", []) or []):
            sdt = (sd.get("side_data_type") or "")
            if "DOVI" in sdt or "Dolby Vision" in sdt:
                dp = sd.get("dv_profile")
                el = sd.get("el_present")
                if dp is not None and str(dp).isdigit():
                    dv_profile = int(dp)
                if el is not None and str(el).isdigit():
                    dv_el_present = int(el)

    a_codecs = [(a.get("codec_name") or "").lower() for a in a_streams if a.get("codec_name")]

    return Analysis(
        path=str(path),
        size_bytes=st.st_size,
        container=container,
        v_codec=v_codec,
        v_bitrate_bps=v_bitrate,
        v_width=v_width,
        v_height=v_height,
        dv_profile=dv_profile,
        dv_el_present=dv_el_present,
        a_codecs=a_codecs,
        should_transcode=False,
        reasons=[],
        duration_sec=duration_sec,
    )

def scan_files(root: Path) -> List[Path]:
    out: List[Path] = []
    if root.is_file():
        return [root] if root.suffix.lower() in VIDEO_EXTS else []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            out.append(p)
    return out


def is_within_root(path: Path, roots: List[Path]) -> bool:
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    for root in roots:
        try:
            root_resolved = root.resolve()
        except Exception:
            root_resolved = root
        if resolved == root_resolved or root_resolved in resolved.parents:
            return True
    return False

# -----------------------
# Language helpers
# -----------------------

LANG_ALIASES = {
    "it": "ita", "ita": "ita", "italian": "ita", "italiano": "ita",
    "en": "eng", "eng": "eng", "english": "eng", "inglese": "eng",
    "es": "spa", "spa": "spa", "spanish": "spa", "spagnolo": "spa",
    "fr": "fra", "fra": "fra", "french": "fra", "francese": "fra",
    "de": "deu", "deu": "deu", "german": "deu", "tedesco": "deu",
    "pt": "por", "por": "por", "portuguese": "por", "portoghese": "por",
}
TARGET_OCR_LANGS = {"ita", "eng"}
LANG_DETECT_SAMPLE_BYTES = 12 * 1024
ITA_HINT_WORDS = {
    "che", "non", "per", "con", "sono", "come", "questo", "questa", "della",
    "delle", "degli", "allora", "anche", "perche", "perché", "quindi", "grazie",
}
ENG_HINT_WORDS = {
    "the", "and", "you", "are", "this", "that", "with", "from", "what", "have",
    "your", "just", "they", "them", "then", "there", "because", "please",
}

def normalize_lang(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("", "und", "unknown", "undefined"):
        return "und"
    s = s.replace("_", "-")
    if s in LANG_ALIASES:
        return LANG_ALIASES[s]
    if re.fullmatch(r"[a-z]{3}([\-][a-z0-9]{2,8})?", s):
        return s
    if re.fullmatch(r"[a-z]{2}-[a-z]{2}", s):
        base = s.split("-", 1)[0]
        return LANG_ALIASES.get(base, "und")
    return "und"

def parse_lang_list(value: str) -> Set[str]:
    langs: Set[str] = set()
    for part in re.split(r"[,;\s]+", value or ""):
        lang = normalize_lang(part)
        if lang != "und":
            langs.add(lang)
    return langs

def detect_lang_from_text(text: str) -> Optional[str]:
    t = (text or "").lower()
    tokens = re.split(r"[^a-z0-9]+", t)
    for tok in tokens:
        if not tok:
            continue
        if tok in LANG_ALIASES:
            return LANG_ALIASES[tok]
        if re.fullmatch(r"[a-z]{2}-[a-z]{2}", tok):
            base = tok.split("-", 1)[0]
            if base in LANG_ALIASES:
                return LANG_ALIASES[base]
    return None

def detect_lang_from_subtitle_payload(text: str) -> Optional[str]:
    """
    Lightweight detector for subtitle bodies, focused on ita/eng.
    Returns ISO639-2 ('ita'/'eng') when confidence is sufficient.
    """
    t = (text or "").lower()
    if not t:
        return None

    # Remove ASS/SSA formatting and subtitle timing noise before tokenization.
    t = re.sub(r"\{\\.*?\}", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"\b\d{1,2}:\d{2}:\d{2}[,.:]\d{2,3}\b", " ", t)
    t = re.sub(r"[^a-zàèéìòù']+", " ", t, flags=re.IGNORECASE)
    tokens = [x for x in t.split() if len(x) >= 2]
    if len(tokens) < 12:
        return None

    ita_score = sum(1 for tok in tokens if tok in ITA_HINT_WORDS)
    eng_score = sum(1 for tok in tokens if tok in ENG_HINT_WORDS)
    max_score = max(ita_score, eng_score)
    if max_score < 3 or abs(ita_score - eng_score) < 2:
        return None
    return "ita" if ita_score > eng_score else "eng"

def sniff_text_track_lang_with_mkvextract(
    mkvextract: str,
    mkv_path: Path,
    track_id: int,
    codec: str,
) -> Optional[str]:
    if not is_text_sub_codec(codec):
        return None
    ext = ".srt"
    c = (codec or "").lower()
    if "ass" in c or "ssa" in c:
        ext = ".ass"
    elif "webvtt" in c or "vtt" in c:
        ext = ".vtt"
    elif "mov_text" in c:
        ext = ".txt"

    td = tempfile.mkdtemp(prefix="mediashrinker-lang-")
    try:
        outp = Path(td) / f"t{track_id:02d}{ext}"
        rc, _, err = run_cmd_capture([mkvextract, str(mkv_path), "tracks", f"{track_id}:{outp}"])
        if rc != 0 or not outp.exists():
            return None
        raw = outp.read_bytes()[:LANG_DETECT_SAMPLE_BYTES]
        txt = raw.decode("utf-8", "replace")
        return detect_lang_from_subtitle_payload(txt)
    except Exception:
        return None
    finally:
        shutil.rmtree(td, ignore_errors=True)

def sniff_external_text_file_lang(path: Path) -> Optional[str]:
    try:
        raw = path.read_bytes()[:LANG_DETECT_SAMPLE_BYTES]
    except Exception:
        return None
    txt = raw.decode("utf-8", "replace")
    return detect_lang_from_subtitle_payload(txt)

def mkv_lang_to_ietf(lang3: str) -> str:
    """For pgsrip filenames/options: wants IETF (en, it, pt-BR...)."""
    l = normalize_lang(lang3)
    if l.startswith("eng") or l == "en":
        return "en"
    if l.startswith("ita") or l == "it":
        return "it"
    if l.startswith("spa") or l == "es":
        return "es"
    if l.startswith("fra") or l == "fr":
        return "fr"
    if l.startswith("deu") or l == "de":
        return "de"
    if l.startswith("por") or l == "pt":
        return "pt"
    return "und"

def mkv_lang_to_mkvmerge_lang(lang3: str) -> str:
    """For mkvmerge --language: keep ISO639-2 3-letter when possible."""
    l = normalize_lang(lang3)
    if l in ("eng", "ita", "spa", "fra", "deu", "por", "und"):
        return l
    # if we accidentally get 2-letter, map back
    if l == "en":
        return "eng"
    if l == "it":
        return "ita"
    return "und"

# -----------------------
# MKV subtitle handling (mkvmerge -J)
# -----------------------

def mkvmerge_json(mkvmerge: str, path: Path) -> Dict[str, Any]:
    rc, out, err = run_cmd_capture([mkvmerge, "-J", str(path)])
    if rc != 0:
        raise RuntimeError(f"mkvmerge -J failed: {err.strip()}")
    return json.loads(out)

def is_pgs_codec(codec: str) -> bool:
    c = (codec or "").lower()
    return ("hdmv_pgs_subtitle" in c) or ("pgssub" in c) or ("hdmv pgs" in c) or ("pgs" in c)

def is_text_sub_codec(codec: str) -> bool:
    c = (codec or "").lower()
    return any(
        x in c
        for x in [
            "subrip",
            "srt",
            "ass",
            "ssa",
            "substationalpha",
            "advancedsubstationalpha",
            "webvtt",
            "utf-8",
            "text",
            "mov_text",
        ]
    )

def safe_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "sub"

@dataclass
class SubtitleTrack:
    id: int
    codec: str
    lang: str
    name: str
    forced: bool
    default: bool

@dataclass
class SubtitleInventory:
    text: List[SubtitleTrack]
    non_text: List[SubtitleTrack]

def read_subtitle_inventory(mkvmerge: str, mkv_path: Path, *, mkvextract: Optional[str] = None) -> SubtitleInventory:
    data = mkvmerge_json(mkvmerge, mkv_path)
    text: List[SubtitleTrack] = []
    non_text: List[SubtitleTrack] = []

    for t in data.get("tracks", []) or []:
        if t.get("type") != "subtitles":
            continue
        tid = int(t.get("id"))
        codec = (t.get("codec") or "")
        props = t.get("properties", {}) or {}
        lang = normalize_lang(props.get("language") or "und")
        name = (props.get("track_name") or "")
        forced = bool(props.get("forced_track") or False)
        default = bool(props.get("default_track") or False)

        if lang == "und":
            guess = detect_lang_from_text(name) or detect_lang_from_text(mkv_path.name)
            if not guess and mkvextract and is_text_sub_codec(codec):
                guess = sniff_text_track_lang_with_mkvextract(mkvextract, mkv_path, tid, codec)
            if guess:
                lang = normalize_lang(guess)

        tr = SubtitleTrack(tid, codec, lang, name, forced, default)

        if is_text_sub_codec(codec):
            text.append(tr)
        else:
            non_text.append(tr)

    return SubtitleInventory(text=text, non_text=non_text)

def find_external_text_langs(nas_src: Path) -> Set[str]:
    langs: Set[str] = set()
    base = nas_src.stem
    try:
        for p in nas_src.parent.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in TEXT_EXTS:
                continue
            if p.stem != base and not p.name.startswith(base + "."):
                continue
            guess = detect_lang_from_text(p.name)
            if not guess:
                guess = sniff_external_text_file_lang(p)
            langs.add(normalize_lang(guess) if guess else "und")
    except Exception:
        pass
    return langs

def find_external_text_subtitles(nas_src: Path) -> List[Tuple[Path, str, str, bool, bool]]:
    tracks: List[Tuple[Path, str, str, bool, bool]] = []
    seen: Set[Path] = set()
    base = nas_src.stem
    try:
        for p in sorted(nas_src.parent.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_file():
                continue
            if p.suffix.lower() not in TEXT_EXTS:
                continue
            if p.stem != base and not p.name.startswith(base + "."):
                continue
            if p in seen:
                continue
            seen.add(p)
            guess = detect_lang_from_text(p.name)
            if not guess:
                guess = sniff_external_text_file_lang(p)
            lang = normalize_lang(guess) if guess else "und"
            lower_name = p.name.lower()
            forced = ".forced" in lower_name or ".forzato" in lower_name
            name = "External"
            if forced:
                name = "External Forced"
            tracks.append((p, lang, name, forced, False))
    except Exception:
        pass
    return tracks

# -----------------------
# Subtitle policy
# -----------------------

@dataclass
class OcrTask:
    track_id: int
    lang: str
    codec: str
    forced: bool
    name: str

@dataclass
class LangAudit:
    lang: str
    ext_text: bool
    keep_text_count: int
    non_text_count: int
    decision_drop_non_text: bool
    decision_ocr: str  # "none" | "all" | "unsupported-vobsub" | "pgs-only-vobsub-skipped"
    ocr_track_ids: List[int]

@dataclass
class SubPlan:
    need_subfix: bool
    drop_ids: List[int]
    keep_ids: List[int]
    ocr_tasks: List[OcrTask]
    external_text_langs: List[str]
    audit: List[LangAudit]

def build_sub_plan(
    inv: SubtitleInventory,
    *,
    external_text_langs: Set[str],
    force_extract_subs: bool = False,
) -> SubPlan:
    non_text_by_lang: Dict[str, List[SubtitleTrack]] = {}
    for t in inv.non_text:
        non_text_by_lang.setdefault(t.lang or "und", []).append(t)

    keep_text_by_lang: Dict[str, List[SubtitleTrack]] = {}
    for t in inv.text:
        keep_text_by_lang.setdefault(t.lang or "und", []).append(t)

    target_has_internal_text = {lang: len(keep_text_by_lang.get(lang, [])) > 0 for lang in TARGET_OCR_LANGS}
    ext_und = "und" in external_text_langs
    target_has_ext_text = {lang: (lang in external_text_langs) or ext_und for lang in TARGET_OCR_LANGS}
    target_has_text = {lang: (target_has_internal_text[lang] or target_has_ext_text[lang]) for lang in TARGET_OCR_LANGS}
    any_target_text = any(target_has_text.values())
    any_text_any_lang = (len(inv.text) > 0) or (len(external_text_langs) > 0)
    avoid_ocr_when_no_target_text = (not any_target_text) and any_text_any_lang

    all_langs = set(non_text_by_lang.keys()) | set(keep_text_by_lang.keys()) | set(external_text_langs) | TARGET_OCR_LANGS
    audit: List[LangAudit] = []
    ocr_tasks: List[OcrTask] = []

    for lang in sorted(all_langs):
        lang = lang or "und"
        ext_text = (lang in external_text_langs) or (lang in TARGET_OCR_LANGS and ext_und)
        keep_cnt = len(keep_text_by_lang.get(lang, []))
        non_text_tracks = non_text_by_lang.get(lang, [])
        non_text_cnt = len(non_text_tracks)
        decision_drop_pgs = False
        decision_ocr = "none"
        ocr_ids: List[int] = []

        if lang in TARGET_OCR_LANGS and non_text_cnt > 0:
            has_text_this_lang = target_has_text.get(lang, False)
            if (not has_text_this_lang) and (not avoid_ocr_when_no_target_text):
                pgs_tracks = [t for t in non_text_tracks if is_pgs_codec(t.codec)]
                unsupported_tracks = [t for t in non_text_tracks if not is_pgs_codec(t.codec)]
                if pgs_tracks:
                    decision_ocr = "pgs-only-vobsub-skipped" if unsupported_tracks else "all"
                    ocr_ids = [t.id for t in pgs_tracks]
                elif unsupported_tracks:
                    decision_ocr = "unsupported-vobsub"
                for tr in pgs_tracks:
                    ocr_tasks.append(OcrTask(tr.id, lang, tr.codec, tr.forced, tr.name))

        audit.append(
            LangAudit(
                lang=lang,
                ext_text=ext_text,
                keep_text_count=keep_cnt,
                non_text_count=non_text_cnt,
                decision_drop_non_text=decision_drop_pgs,
                decision_ocr=decision_ocr,
                ocr_track_ids=ocr_ids,
            )
        )

    need_subfix = bool(external_text_langs) or bool(ocr_tasks) or bool(force_extract_subs)
    return SubPlan(
        need_subfix=need_subfix,
        drop_ids=[],
        keep_ids=[t.id for t in inv.text] + [t.id for t in inv.non_text],
        ocr_tasks=ocr_tasks,
        external_text_langs=sorted(external_text_langs),
        audit=audit,
    )

def format_audit_lines(audit: List[LangAudit]) -> List[str]:
    lines: List[str] = []
    for a in audit:
        if a.non_text_count == 0 and a.keep_text_count == 0 and not a.ext_text:
            continue
        lines.append(
            f"       [AUDIT lang={a.lang}] ext_text={a.ext_text} "
            f"keep_text={a.keep_text_count} non_text={a.non_text_count} "
            f"=> drop_non_text={a.decision_drop_non_text} ocr={a.decision_ocr} ocr_ids={a.ocr_track_ids}"
        )
    return lines

# -----------------------
# mkvextract helpers
# -----------------------

def run_mkvextract_with_progress(cmd: List[str], out_file: Path, prefix: str) -> Tuple[int, str]:
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    register_proc(p)
    spinner = ["|", "/", "-", "\\"]
    si = 0

    last_t = time.time()
    last_sz = 0
    while True:
        if STOP_REQUESTED.is_set():
            try:
                p.terminate()
            except Exception:
                pass
        rc = p.poll()
        now = time.time()

        if now - last_t >= 0.2:
            sz = 0
            try:
                if out_file.exists():
                    sz = out_file.stat().st_size
            except Exception:
                sz = 0

            dt = max(now - last_t, 0.001)
            dsz = max(sz - last_sz, 0)
            mb_s = (dsz / dt) / 1024 / 1024
            print_spinner(prefix, spinner[si], sz, mb_s if sz > 0 else None)

            si = (si + 1) % len(spinner)
            last_t = now
            last_sz = sz

        if rc is not None:
            break

    _, err_b = p.communicate()
    unregister_proc(p)
    try:
        sz = out_file.stat().st_size if out_file.exists() else 0
    except Exception:
        sz = 0
    print_spinner(prefix, "*", sz, None)
    if PROGRESS_SLOTS <= 1:
        print()

    err = (err_b.decode("utf-8", "replace") if err_b else "").strip()
    return int(rc), err

def extract_pgs_for_pgsrip(mkvextract: str, local_src: Path, tr: SubtitleTrack, out_dir: Path) -> Path:
    """
    Extract a PGS track to a filename that pgsrip will ACCEPT:
      <base>.t08.en.sup  (IETF language is the last token before .sup)
    """
    if not is_pgs_codec(tr.codec):
        raise RuntimeError(f"pgsrip OCR supports PGS only; track {tr.id} codec={tr.codec} is unsupported")
    base = local_src.stem
    lang_ietf = mkv_lang_to_ietf(tr.lang or "und")
    if lang_ietf == "und":
        raise RuntimeError(f"unsupported OCR lang for track {tr.id}: {tr.lang}")
    outp = out_dir / f"{base}.t{tr.id:02d}.{lang_ietf}.sup"
    if outp.exists():
        outp = out_dir / f"{base}.t{tr.id:02d}.{lang_ietf}.{int(time.time())}.sup"

    cmd = [mkvextract, str(local_src), "tracks", f"{tr.id}:{outp}"]
    rc, err = run_mkvextract_with_progress(cmd, outp, prefix=f"Extract PGS t{tr.id:02d} {local_src.name}")
    if rc != 0:
        raise RuntimeError(f"mkvextract failed for track {tr.id}: {err.strip()}")
    return outp

# -----------------------
# OCR via pgsrip
# -----------------------

def pgsrip_sup_to_srt(sup_path: Path, *, pgsrip_bin: str, tessdata_prefix: str, ocr_langs_ietf: List[str]) -> Path:
    """
    Runs pgsrip on a .sup, returns the produced .srt path.
    IMPORTANT:
      - OCR languages are selected per track (ita -> it, eng -> en).
      - The sup filename MUST end with .en.sup / .it.sup etc, otherwise pgsrip may filter it out.
    """
    env = os.environ.copy()
    env["TESSDATA_PREFIX"] = tessdata_prefix
    if os.name == "nt":
        tess_bin = Path(r"C:\Program Files\Tesseract-OCR")
        if tess_bin.exists():
            env["PATH"] = str(tess_bin) + os.pathsep + env.get("PATH", "")

    langs = [x for x in ocr_langs_ietf if x in ("it", "en")]
    if not langs:
        raise RuntimeError("no supported OCR language selected for pgsrip")
    cmd: List[str] = [pgsrip_bin]
    for lang in langs:
        cmd += ["-l", lang]
    cmd += [str(sup_path)]
    cmd_str = " ".join(shlex.quote(x) for x in cmd)
    rc, out, err = run_cmd_capture(cmd, env=env)
    if rc != 0:
        raise RuntimeError(f"pgsrip failed rc={rc}: {(err or out).strip()}")

    expected = sup_path.with_suffix(".srt")
    if expected.exists() and expected.stat().st_size > 0:
        return expected

    # fallback: find newest srt in same dir
    cand = list(sup_path.parent.glob(sup_path.stem + "*.srt")) + list(sup_path.parent.glob("*.srt"))
    cand = [p for p in cand if p.is_file()]
    if not cand:
        def _clip(txt: str, max_len: int = 2000) -> str:
            t = (txt or "").strip()
            if len(t) <= max_len:
                return t
            return t[: max_len - 3] + "..."
        dir_entry_names = sorted(p.name for p in itertools.islice(sup_path.parent.glob("*"), 80))
        raise RuntimeError(
            "pgsrip ran but no .srt found; "
            f"cmd={cmd_str}; stdout={_clip(out)}; stderr={_clip(err)}; dir_files={dir_entry_names}"
        )
    srt = max(cand, key=lambda p: p.stat().st_mtime)
    if srt.stat().st_size == 0:
        raise RuntimeError("pgsrip produced an empty .srt")
    return srt

def infer_non_text_lang_via_probe_ocr(
    *,
    mkvextract: str,
    mkv_path: Path,
    tr: SubtitleTrack,
    pgsrip_bin: str,
    tessdata_prefix: str,
) -> Optional[str]:
    """
    Best-effort language inference for non-text tracks tagged as und.
    Currently supported for PGS tracks only.
    """
    if not is_pgs_codec(tr.codec):
        return None

    td = tempfile.mkdtemp(prefix="mediashrinker-und-probe-")
    try:
        base = safe_name(mkv_path.stem)
        sup = Path(td) / f"{base}.t{tr.id:02d}.en.sup"
        rc, _, err = run_cmd_capture([mkvextract, str(mkv_path), "tracks", f"{tr.id}:{sup}"])
        if rc != 0 or not sup.exists():
            return None
        srt = pgsrip_sup_to_srt(
            sup,
            pgsrip_bin=pgsrip_bin,
            tessdata_prefix=tessdata_prefix,
            ocr_langs_ietf=["en", "it"],
        )
        sample = srt.read_bytes()[:LANG_DETECT_SAMPLE_BYTES].decode("utf-8", "replace")
        guessed = detect_lang_from_subtitle_payload(sample)
        if guessed in TARGET_OCR_LANGS:
            return guessed
        return None
    except Exception:
        return None
    finally:
        shutil.rmtree(td, ignore_errors=True)

# -----------------------
# mkvmerge remux helpers
# -----------------------

def mkvmerge_build_final_from_source(
    mkvmerge: str,
    *,
    local_src: Path,
    out_final: Path,
    keep_sub_ids: List[int],
    add_srt: List[Tuple[Path, str, str, bool, bool]],  # (path, lang, name, forced, default)
) -> None:
    cmd: List[str] = [mkvmerge, "-o", str(out_final)]

    if keep_sub_ids:
        cmd += ["--subtitle-tracks", ",".join(str(i) for i in keep_sub_ids)]
    else:
        cmd += ["--no-subtitles"]

    cmd += [str(local_src)]

    for (srt_path, lang, name, forced, default) in add_srt:
        mkv_lang = mkv_lang_to_mkvmerge_lang(lang or "und")
        nm = name or "OCR"
        cmd += [
            "--language", f"0:{mkv_lang}",
            "--track-name", f"0:{nm}",
            "--forced-track", f"0:{'yes' if forced else 'no'}",
            "--default-track", f"0:{'yes' if default else 'no'}",
            str(srt_path),
        ]

    rc, out, err = run_cmd_capture(cmd)
    if rc != 0:
        raise RuntimeError(f"mkvmerge failed: {(err or out).strip()}")

def mkvmerge_build_final_from_encoded(
    mkvmerge: str,
    *,
    encoded_in: Path,
    original_local: Path,
    out_final: Path,
    keep_sub_ids_from_original: List[int],
    add_srt: List[Tuple[Path, str, str, bool, bool]],
) -> None:
    cmd: List[str] = [mkvmerge, "-o", str(out_final), str(encoded_in)]

    if keep_sub_ids_from_original:
        cmd += [
            "--no-video", "--no-audio", "--no-chapters",
            "--subtitle-tracks", ",".join(str(i) for i in keep_sub_ids_from_original),
            "--no-global-tags",
            str(original_local),
        ]

    for (srt_path, lang, name, forced, default) in add_srt:
        mkv_lang = mkv_lang_to_mkvmerge_lang(lang or "und")
        nm = name or "OCR"
        cmd += [
            "--language", f"0:{mkv_lang}",
            "--track-name", f"0:{nm}",
            "--forced-track", f"0:{'yes' if forced else 'no'}",
            "--default-track", f"0:{'yes' if default else 'no'}",
            str(srt_path),
        ]

    rc, out, err = run_cmd_capture(cmd)
    if rc != 0:
        raise RuntimeError(f"mkvmerge final remux failed: {(err or out).strip()}")

def mkvmerge_build_sub_source_mkv(mkvmerge: str, *, src: Path, out_mkv: Path) -> None:
    rc, out, err = run_cmd_capture([mkvmerge, "-o", str(out_mkv), str(src)])
    if rc != 0:
        raise RuntimeError(f"mkvmerge sub-source remux failed: {(err or out).strip()}")


def is_matroska_structure_error(msg: str) -> bool:
    t = (msg or "").lower()
    needles = [
        "error in the matroska file structure",
        "resync failed",
        "no valid matroska level 1 element",
        "invalid as first byte of an ebml",
        "ebml",
    ]
    return any(x in t for x in needles)

# -----------------------
# FFmpeg Transcode
# -----------------------

def send_ntfy(url: str, title: str, message: str) -> None:
    """Invia una notifica push a un server ntfy (o compatibile)."""
    if not url:
        return
    try:
        import urllib.request as _req
        data = message.encode("utf-8")
        req = _req.Request(url, data=data, method="POST")
        req.add_header("Title", title)
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        _req.urlopen(req, timeout=5)
    except Exception:
        pass


def ffmpeg_cmd(
    ffmpeg_bin: str,
    src: Path,
    dst_local: Path,
    *,
    encoder: str,
    cq: int,
    preset: str = "p5",
    multipass: bool,
    maxrate_kbps: Optional[int] = None,
    vaapi_device: str = "/dev/dri/renderD128",
) -> List[str]:
    if encoder == "hevc_vaapi":
        # Software decode → VAAPI encode (8-bit NV12; universalmente compatibile)
        cmd = [
            ffmpeg_bin,
            "-hide_banner", "-y",
            "-vaapi_device", vaapi_device,
            "-i", str(src),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-sn",
            "-map_metadata", "0",
            "-map_chapters", "0",
            "-vf", "format=nv12,hwupload",
            "-c:v", "hevc_vaapi",
            "-qp", str(int(cq)),
            "-c:a", "copy",
            "-max_muxing_queue_size", "4096",
        ]
        if maxrate_kbps is not None and int(maxrate_kbps) > 0:
            cmd += ["-b:v", f"{maxrate_kbps}k"]
        cmd += [str(dst_local)]
        return cmd

    cmd = [
        ffmpeg_bin,
        "-hide_banner", "-y",
        "-i", str(src),
        "-map", "0:v:0",
        "-map", "0:a?",
        "-sn",
        "-map_metadata", "0",
        "-map_chapters", "0",
        "-c:v", encoder,
        "-pix_fmt", "p010le",
        "-c:a", "copy",
        "-max_muxing_queue_size", "4096",
    ]
    if encoder == "hevc_nvenc":
        cmd += ["-rc", "vbr", "-cq", str(int(cq)), "-b:v", "0"]
        if maxrate_kbps is not None and int(maxrate_kbps) > 0:
            mr = int(maxrate_kbps)
            cmd += ["-maxrate", f"{mr}k", "-bufsize", f"{max(2 * mr, 2000)}k"]
        cmd += ["-preset", preset, "-multipass", "fullres" if multipass else "disabled"]
    else:
        # libx265 o altri encoder software
        cmd += ["-crf", str(int(cq)), "-preset", preset]
    cmd += [str(dst_local)]
    return cmd

def run_ffmpeg_with_progress_tty(
    cmd: List[str],
    prefix: str,
    log_path: Path,
    *,
    duration_sec: Optional[float] = None,
) -> Tuple[int, float]:
    if not HAVE_PTY or os.name == "nt":
        return run_ffmpeg_with_progress_pipe(cmd, prefix=prefix, log_path=log_path, duration_sec=duration_sec)

    t0 = time.time()
    master_fd, slave_fd = pty.openpty()
    re_speed = re.compile(r"speed=\s*([0-9.]+x)")
    re_time = re.compile(r"time=\s*([0-9:.]+)")

    with open(log_path, "w", encoding="utf-8") as logf:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=slave_fd, stderr=slave_fd, close_fds=True)
        register_proc(p)
        os.close(slave_fd)

        buf = ""
        last_update = time.time()
        last_draw = 0.0
        spinner = ["|", "/", "-", "\\"]
        spin_i = 0
        last_speed = ""
        last_media_time = ""

        while True:
            if STOP_REQUESTED.is_set():
                try:
                    p.terminate()
                except Exception:
                    pass
            r, _, _ = select.select([master_fd], [], [], 0.2)
            if master_fd in r:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    data = b""
                if data:
                    chunk = data.decode("utf-8", "replace")
                    logf.write(chunk)
                    logf.flush()
                    buf += chunk

                    while True:
                        m = re.search(r"[\r\n]", buf)
                        if not m:
                            break
                        rec = buf[:m.start()]
                        buf = buf[m.end():]
                        m2 = re_speed.search(rec)
                        if m2:
                            last_speed = m2.group(1)
                            last_update = time.time()
                        m3 = re_time.search(rec)
                        if m3:
                            last_media_time = m3.group(1)
                            last_update = time.time()

                        now = time.time()
                        draw_interval = 2.0 if PROGRESS_SLOTS > 1 else 0.4
                        if (not STOP_REQUESTED.is_set()) and (now - last_draw >= draw_interval):
                            spin_i = (spin_i + 1) % len(spinner)
                            eta_s = estimate_encode_eta_seconds(
                                duration_sec=duration_sec,
                                out_time_str=last_media_time,
                                speed_str=last_speed,
                            )
                            eta_txt = f" eta={fmt_eta(eta_s)}" if eta_s is not None else ""
                            msg = f"{prefix} {spinner[spin_i]} (encoding... {last_speed} t={last_media_time}{eta_txt})"
                            if PROGRESS_INLINE:
                                print(f"\r{msg}", end="", flush=True)
                            elif PROGRESS_SLOTS > 1 and get_thread_progress_slot() is not None:
                                _update_progress_slot(get_thread_progress_slot() or 1, msg)
                            else:
                                print(msg, flush=True)
                            last_draw = now

            if (not STOP_REQUESTED.is_set()) and (time.time() - last_update > 2.0):
                spin_i = (spin_i + 1) % len(spinner)
                eta_s = estimate_encode_eta_seconds(
                    duration_sec=duration_sec,
                    out_time_str=last_media_time,
                    speed_str=last_speed,
                )
                eta_txt = f" eta={fmt_eta(eta_s)}" if eta_s is not None else ""
                msg = f"{prefix} {spinner[spin_i]} (encoding... {last_speed} t={last_media_time}{eta_txt})"
                if PROGRESS_INLINE:
                    print(f"\r{msg}", end="", flush=True)
                elif PROGRESS_SLOTS > 1 and get_thread_progress_slot() is not None:
                    _update_progress_slot(get_thread_progress_slot() or 1, msg)
                else:
                    print(msg, flush=True)
                last_update = time.time()

            rc = p.poll()
            if rc is not None:
                break

        try:
            while True:
                data = os.read(master_fd, 4096)
                if not data:
                    break
                logf.write(data.decode("utf-8", "replace"))
        except Exception:
            pass

    os.close(master_fd)
    unregister_proc(p)
    if PROGRESS_SLOTS <= 1:
        print()
    return int(rc), time.time() - t0


def run_ffmpeg_with_progress_pipe(
    cmd: List[str],
    prefix: str,
    log_path: Path,
    *,
    duration_sec: Optional[float] = None,
) -> Tuple[int, float]:
    """
    Cross-platform ffmpeg progress parser (works on Windows too).
    Uses '-progress pipe:2 -nostats' and parses key/value lines.
    """
    t0 = time.time()
    cmd2 = list(cmd)
    if len(cmd2) >= 1:
        out_arg = cmd2[-1]
        cmd2 = cmd2[:-1] + ["-progress", "pipe:2", "-nostats", out_arg]

    with open(log_path, "w", encoding="utf-8") as logf:
        p = subprocess.Popen(
            cmd2,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        register_proc(p)
        last_speed = ""
        last_media_time = ""
        spinner = ["|", "/", "-", "\\"]
        spin_i = 0
        last_draw = 0.0

        try:
            assert p.stderr is not None
            while True:
                if STOP_REQUESTED.is_set():
                    try:
                        p.terminate()
                    except Exception:
                        pass
                line = p.stderr.readline()
                if line:
                    logf.write(line)
                    logf.flush()
                    x = line.strip()
                    if x.startswith("speed="):
                        last_speed = x.split("=", 1)[1].strip()
                    elif x.startswith("out_time="):
                        last_media_time = x.split("=", 1)[1].strip()

                    now = time.time()
                    draw_interval = 2.0 if PROGRESS_SLOTS > 1 else 0.4
                    if (not STOP_REQUESTED.is_set()) and (now - last_draw >= draw_interval):
                        spin_i = (spin_i + 1) % len(spinner)
                        eta_s = estimate_encode_eta_seconds(
                            duration_sec=duration_sec,
                            out_time_str=last_media_time,
                            speed_str=last_speed,
                        )
                        eta_txt = f" eta={fmt_eta(eta_s)}" if eta_s is not None else ""
                        msg = f"{prefix} {spinner[spin_i]} (encoding... {last_speed} t={last_media_time}{eta_txt})"
                        if PROGRESS_INLINE:
                            print(f"\r{msg}", end="", flush=True)
                        elif PROGRESS_SLOTS > 1 and get_thread_progress_slot() is not None:
                            _update_progress_slot(get_thread_progress_slot() or 1, msg)
                        else:
                            print(msg, flush=True)
                        last_draw = now
                else:
                    rc = p.poll()
                    if rc is not None:
                        break
                    time.sleep(0.1)
        finally:
            unregister_proc(p)

    if PROGRESS_SLOTS <= 1:
        print()
    return int(p.returncode or 0), time.time() - t0

# -----------------------
# NAS swap + cleanup
# -----------------------

def atomic_swap_on_nas(original: Path, local_out: Path, *, copy_prefix: str = "Copy to NAS") -> Tuple[Optional[int], Optional[str]]:
    nas_tmp = original.with_suffix(original.suffix + ".tmp")
    nas_bak = original.with_suffix(original.suffix + ".bak")

    try:
        if nas_tmp.exists():
            nas_tmp.unlink()

        if PROGRESS_SLOTS <= 1:
            print("Copio sul NAS e faccio swap atomico...")
        copy_with_progress(local_out, nas_tmp, prefix=copy_prefix)

        if nas_bak.exists():
            nas_bak.unlink()
        original.rename(nas_bak)
        nas_tmp.rename(original)

        return local_out.stat().st_size, None
    except Exception as e:
        err = str(e)
        try:
            if not original.exists() and nas_bak.exists():
                nas_bak.rename(original)
        except Exception as rb:
            err = f"{err}; rollback failed: {rb}"
        try:
            if nas_tmp.exists():
                nas_tmp.unlink()
        except Exception:
            pass
        return None, err

def cleanup_baks(roots: List[Path]) -> int:
    deleted = 0
    for root in roots:
        if root.is_file():
            candidates = [root] if root.suffix.lower() == ".bak" else []
        else:
            candidates = list(root.rglob("*.bak"))
        for p in candidates:
            try:
                p.unlink()
                deleted += 1
            except Exception:
                pass
    return deleted

def cleanup_staging_dir(staging_dir: Path) -> int:
    deleted = 0
    if not staging_dir.exists():
        return 0
    for p in staging_dir.iterdir():
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink()
            deleted += 1
        except Exception:
            pass
    return deleted

# -----------------------
# UI prompts (with defaults from cfg)
# -----------------------

def prompt_library_choice(default_choice: str) -> str:
    print("\nCosa vuoi scansionare?")
    print("  1) Movies")
    print("  2) Series")
    print("  3) Entrambi")
    while True:
        c = input(f"Scelta [1-3] (ENTER={default_choice}): ").strip()
        if c == "":
            return default_choice
        if c in ("1", "2", "3"):
            return c

def prompt_mode(default_choice: str) -> Tuple[bool, bool, bool, str]:
    print("\nCosa vuoi fare?")
    print("  1) PLAN (solo simulazione)")
    print("  2) RUN (elabora)")
    print("  3) RUN + cancella subito il .bak dopo ogni transcode/swap riuscito")
    print("  4) SOLO CLEANUP .bak (nessun transcode/subfix)")
    while True:
        c = input(f"Scelta [1-4] (ENTER={default_choice}): ").strip()
        if c == "":
            c = default_choice
        if c == "1":
            return (False, False, False, "1")
        if c == "2":
            return (True, False, False, "2")
        if c == "3":
            return (True, True, False, "3")
        if c == "4":
            return (False, False, True, "4")

def prompt_jobs(default_jobs: int) -> int:
    while True:
        x = input(f"\nNumero job paralleli (ENTER={default_jobs}): ").strip()
        if x == "":
            return max(1, int(default_jobs))
        if x.isdigit() and int(x) >= 1:
            return int(x)

def prompt_vb(default_kbps: int) -> int:
    print("\nBitrate target (solo se fai transcode):")
    print("  1) 30000 kbps")
    print("  2) 40000 kbps")
    print("  3) 50000 kbps")
    print("  4) Personalizzato")
    print(f"  ENTER = {default_kbps} kbps (ultimo usato)")
    while True:
        c = input("Scelta [1-4 o ENTER]: ").strip()
        if c == "":
            return int(default_kbps)
        if c == "1":
            return 30000
        if c == "2":
            return 40000
        if c == "3":
            return 50000
        if c == "4":
            x = input("Inserisci vb-kbps (es 35000): ").strip()
            if x.isdigit() and int(x) > 1000:
                return int(x)

def prompt_yes_no(msg: str, default_yes: bool = True) -> bool:
    d = "Y/n" if default_yes else "y/N"
    while True:
        x = input(f"{msg} [{d}]: ").strip().lower()
        if x == "" and default_yes:
            return True
        if x == "" and not default_yes:
            return False
        if x in ("y", "yes", "s", "si"):
            return True
        if x in ("n", "no"):
            return False

def prompt_text(msg: str, default_value: str) -> str:
    x = input(f"{msg} (ENTER={default_value}): ").strip()
    return x if x else default_value

def prompt_int(msg: str, default_value: int, *, min_value: int = 1) -> int:
    while True:
        x = input(f"{msg} (ENTER={default_value}): ").strip()
        if x == "":
            return int(default_value)
        if x.isdigit() and int(x) >= min_value:
            return int(x)

def prompt_float(msg: str, default_value: float, *, min_value: float = 0.0) -> float:
    while True:
        x = input(f"{msg} (ENTER={default_value}): ").strip()
        if x == "":
            return float(default_value)
        try:
            v = float(x)
            if v >= min_value:
                return v
        except Exception:
            pass

def prompt_choice(msg: str, options: List[str], default_value: str) -> str:
    opts = "/".join(options)
    while True:
        x = input(f"{msg} [{opts}] (ENTER={default_value}): ").strip().lower()
        if x == "":
            return default_value
        if x in options:
            return x

def normalize_encoder_name(s: str) -> str:
    x = (s or "").strip().lower()
    if x in ("nvenc_h265_10bit", "hevc_nvenc", "h265_nvenc"):
        return "hevc_nvenc"
    if x in ("libx265", "x265", "hevc"):
        return "libx265"
    return s

def normalize_input_path(p: str) -> Path:
    """
    Normalize user-provided paths without forcing expensive/brittle network resolution.
    On Windows UNC paths (\\\\server\\share\\...), avoid Path.resolve() to keep CLI responsive.
    """
    x = Path(p).expanduser()
    sx = str(x)
    if os.name == "nt" and sx.startswith("\\\\"):
        return x
    try:
        return x.resolve()
    except Exception:
        return x

# -----------------------
# Data structures for plan/run reporting
# -----------------------

@dataclass
class PlanItem:
    path: str
    need_transcode: bool
    need_subfix: bool
    reasons_video: List[str]
    subtitle_plan: Optional[Dict[str, Any]]
    sub_audit: List[Dict[str, Any]]
    external_text_langs: List[str]
    ocr_tasks: List[Dict[str, Any]]

@dataclass
class JobResult:
    path: str
    action: str
    reasons: List[str]
    elapsed_sec: float
    hb_exit_code: Optional[int]
    output_bytes: Optional[int]
    error: Optional[str]

# -----------------------
# Main
# -----------------------

def main() -> int:
    global RUN_ACTIVE
    STOP_REQUESTED.clear()
    RUN_ACTIVE = False
    ap = argparse.ArgumentParser(
        description=(
            "MediaShrinker: scan & shrink media with a Plex-friendly, space-first policy. "
            "PLAN scans on NAS (ffprobe+mkvmerge). RUN copies only required files to SSD, "
            "does: preserve original subtitles, optional OCR for missing ita/eng text during transcode, "
            "optional transcode via ffmpeg, remux with mkvmerge, then atomic swap on NAS, optional .bak cleanup."
        )
    )
    ap.add_argument("--plan", action="store_true", help="Run non-interactive: plan only (skip interactive mode).")
    ap.add_argument("--run", action="store_true", help="Run non-interactive: execute processing (skip interactive mode).")
    ap.add_argument("--delete-bak", action="store_true", help="Run non-interactive: delete .bak at end.")
    ap.add_argument("--cleanup-only", action="store_true", help="Run non-interactive: delete only .bak files and exit.")
    ap.add_argument("--yes", action="store_true", help="No confirmation prompt in RUN.")
    ap.add_argument("--library", choices=["movies", "series", "both"], default=None, help="Non-interactive library choice.")
    ap.add_argument("--movies-root", default=str(DEFAULT_MOVIES_ROOT))
    ap.add_argument("--series-root", default=str(DEFAULT_SERIES_ROOT))
    ap.add_argument("--staging-dir", default=str(DEFAULT_STAGING_DIR))
    ap.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR))
    ap.add_argument("--bitrate-threshold-mbps", type=float, default=55.0)
    ap.add_argument("--bitrate-4k-mbps", type=float, default=45.0)
    ap.add_argument("--encoder", default="hevc_nvenc")
    ap.add_argument("--vb-kbps", type=int, default=30000)
    ap.add_argument("--no-multipass", action="store_true")
    ap.add_argument("--extract-pgs", action="store_true", default=True)
    ap.add_argument("--no-extract-pgs", dest="extract_pgs", action="store_false")
    ap.add_argument("--add-external-text-subs", action="store_true", default=None, help="Mux matching external text subtitles when a file is already being processed.")
    ap.add_argument("--no-add-external-text-subs", dest="add_external_text_subs", action="store_false", help="Do not mux external text subtitles.")
    ap.add_argument("--ocr-engine", choices=["pgsrip", "none"], default=None)
    ap.add_argument("--ocr-target-langs", default=None, help="Comma-separated OCR target languages, e.g. ita,eng,spa.")
    ap.add_argument("--pgsrip-bin", default=None)
    ap.add_argument("--tessdata-prefix", default=None)
    ap.add_argument("--jobs", type=int, default=1, help="Numero massimo di file processati in parallelo durante RUN.")
    ap.add_argument("--encoding-profile", default=None,
                    choices=list(ENCODING_PROFILES.keys()),
                    help=f"Profilo di encoding: {', '.join(ENCODING_PROFILES.keys())} (default: {DEFAULT_PROFILE}).")
    ap.add_argument("--notify-url", default=None,
                    help="URL ntfy (o compatibile) per notifiche push al termine del job. Es: https://ntfy.sh/mio-canale")
    ap.add_argument("--no-save-config", action="store_true", help="Non salvare ~/.mediashrinker.json")
    ap.add_argument("--force-extract-subs", action="store_true", default=False,
                    help="Forza l'estrazione/OCR dei sottotitoli anche se il video non richiede transcode.")
    ap.add_argument("--resume-run-id", type=int, default=None, metavar="RUN_ID",
                    help="Riprendi un run interrotto: salta i file già transcodificati/subfixed nel run indicato.")
    ap.add_argument("--target-path", default=None,
                    help="Limita il job a un singolo file o a una singola cartella titolo.")
    args = ap.parse_args()

    ffprobe = which_or("ffprobe")
    ffmpeg_bin = which_or("ffmpeg")
    mkvmerge = which_or("mkvmerge")
    mkvextract = which_or("mkvextract")

    movies_root = normalize_input_path(args.movies_root)
    series_root = normalize_input_path(args.series_root)
    staging_dir = normalize_input_path(args.staging_dir)
    report_dir  = normalize_input_path(args.report_dir)

    staging_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    cfg = load_cfg(DEFAULT_CFG_PATH)

    # library selection
    if args.library:
        lib_choice = {"movies": "1", "series": "2", "both": "3"}[args.library]
    elif (args.run or args.plan) and args.yes:
        lib_choice = cfg.library_choice
    else:
        lib_choice = prompt_library_choice(cfg.library_choice)

    cleanup_only = False
    interactive_session = not (args.plan or args.run or args.delete_bak or args.cleanup_only)
    if args.cleanup_only:
        run_mode = False
        delete_bak_at_end = False
        cleanup_only = True
        mode_choice = "4"
    elif args.plan:
        run_mode = False
        delete_bak_at_end = False
        mode_choice = "1"
    elif args.run or args.delete_bak:
        run_mode = bool(args.run)
        delete_bak_at_end = bool(args.delete_bak)
        mode_choice = "3" if delete_bak_at_end else ("2" if run_mode else "1")
    else:
        run_mode, delete_bak_at_end, cleanup_only, mode_choice = prompt_mode(cfg.mode_choice)

    bitrate_threshold_mbps = float(args.bitrate_threshold_mbps)
    bitrate_4k_mbps = float(args.bitrate_4k_mbps)
    no_multipass = bool(args.no_multipass)
    extract_pgs = bool(args.extract_pgs)
    add_external_text_subs = cfg.add_external_text_subs if args.add_external_text_subs is None else bool(args.add_external_text_subs)

    ocr_engine = args.ocr_engine if args.ocr_engine is not None else cfg.ocr_engine
    ocr_target_langs = args.ocr_target_langs if args.ocr_target_langs is not None else cfg.ocr_target_langs
    global TARGET_OCR_LANGS
    parsed_ocr_langs = parse_lang_list(ocr_target_langs)
    if parsed_ocr_langs:
        TARGET_OCR_LANGS = parsed_ocr_langs
        ocr_target_langs = ",".join(sorted(TARGET_OCR_LANGS))
    else:
        TARGET_OCR_LANGS = {"ita", "eng"}
        ocr_target_langs = "ita,eng"
    pgsrip_bin = args.pgsrip_bin if args.pgsrip_bin is not None else cfg.pgsrip_bin
    tessdata_prefix = args.tessdata_prefix if args.tessdata_prefix is not None else cfg.tessdata_prefix
    ocr_engine, pgsrip_bin, tessdata_prefix = normalize_windows_ocr_values(
        ocr_engine,
        pgsrip_bin,
        tessdata_prefix,
    )

    encoder = normalize_encoder_name(str(args.encoder))
    vb_kbps = int(args.vb_kbps)
    encoding_profile = args.encoding_profile if args.encoding_profile is not None else cfg.encoding_profile
    notify_url = (args.notify_url or "").strip()
    save_config = not args.no_save_config
    auto_yes = bool(args.yes)

    if args.jobs != 1:
        jobs = max(1, int(args.jobs))
    else:
        jobs = max(1, int(cfg.jobs))

    if interactive_session:
        if prompt_yes_no("Aprire impostazioni avanzate?", default_yes=False):
            movies_root = normalize_input_path(prompt_text("Movies root", str(movies_root)))
            series_root = normalize_input_path(prompt_text("Series root", str(series_root)))
            staging_dir = normalize_input_path(prompt_text("Staging dir", str(staging_dir)))
            report_dir = normalize_input_path(prompt_text("Report dir", str(report_dir)))
            bitrate_threshold_mbps = prompt_float("Soglia bitrate Mbps", bitrate_threshold_mbps, min_value=0.0)
            bitrate_4k_mbps = prompt_float("Soglia bitrate 4K Mbps", bitrate_4k_mbps, min_value=0.0)
            encoder = normalize_encoder_name(prompt_text("Encoder video", encoder))
            no_multipass = not prompt_yes_no("Abilitare multipass?", default_yes=(not no_multipass))
            extract_pgs = prompt_yes_no("Abilitare estrazione PGS?", default_yes=extract_pgs)
            add_external_text_subs = prompt_yes_no("Muxare sottotitoli testuali esterni durante i transcode?", default_yes=add_external_text_subs)
            ocr_engine = prompt_choice("OCR engine", ["pgsrip", "none"], ocr_engine)
            ocr_target_langs = prompt_text("Lingue target OCR (csv)", ocr_target_langs)
            parsed_ocr_langs = parse_lang_list(ocr_target_langs)
            if parsed_ocr_langs:
                TARGET_OCR_LANGS = parsed_ocr_langs
                ocr_target_langs = ",".join(sorted(TARGET_OCR_LANGS))
            pgsrip_bin = prompt_text("Percorso pgsrip", pgsrip_bin)
            tessdata_prefix = prompt_text("TESSDATA_PREFIX", tessdata_prefix)
            auto_yes = prompt_yes_no("Saltare conferme (equiv. --yes)?", default_yes=auto_yes)
            save_config = prompt_yes_no("Salvare configurazione in ~/.mediashrinker.json?", default_yes=save_config)

        if run_mode:
            jobs = prompt_jobs(jobs)

    roots: List[Path] = []
    if lib_choice in ("1", "3"):
        roots.append(movies_root)
    if lib_choice in ("2", "3"):
        roots.append(series_root)
    target_path: Optional[Path] = None
    if args.target_path:
        target_path = normalize_input_path(args.target_path)
        if not target_path.exists():
            print(f"[ERR] target path not found: {target_path}")
            return 2
        if not is_within_root(target_path, roots):
            print(f"[ERR] target path is outside selected libraries: {target_path}")
            return 2

    staging_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    cfg.library_choice = lib_choice
    cfg.mode_choice = mode_choice
    cfg.bitrate_threshold_mbps = bitrate_threshold_mbps
    cfg.bitrate_4k_mbps = bitrate_4k_mbps
    cfg.no_multipass = no_multipass
    cfg.extract_pgs = extract_pgs
    cfg.add_external_text_subs = bool(add_external_text_subs)
    cfg.ocr_engine = ocr_engine
    cfg.ocr_target_langs = ocr_target_langs
    cfg.pgsrip_bin = pgsrip_bin
    cfg.tessdata_prefix = tessdata_prefix
    cfg.vb_kbps = int(vb_kbps)  # legacy compat; non usato nella policy auto standard
    cfg.jobs = jobs
    global PROGRESS_INLINE
    PROGRESS_INLINE = (jobs == 1)

    ts = time.strftime("%Y%m%d-%H%M%S")
    run_t0 = time.time()
    log_path  = report_dir / f"run-{ts}.log"
    json_path = report_dir / f"run-{ts}.json"
    analyses: Dict[str, Analysis] = {}

    log_lock = threading.Lock()

    def _console_log_allowed(s: str) -> bool:
        if s.startswith("[STOP]"):
            return True
        if not (PARALLEL_FIXED_ROWS and RUN_ACTIVE):
            return True
        return threading.current_thread() is threading.main_thread()

    def log(s: str) -> None:
        with log_lock:
            if _console_log_allowed(s):
                with CONSOLE_LOCK:
                    print(s)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(s + "\n")

    def cfg_snapshot() -> Dict[str, Any]:
        return {
            "roots": [str(r) for r in roots],
            "staging_dir": str(staging_dir),
            "report_dir": str(report_dir),
            "encoder": encoder,
            "jobs": jobs,
            "bitrate_threshold_mbps": bitrate_threshold_mbps,
            "bitrate_4k_mbps": bitrate_4k_mbps,
            "no_multipass": bool(no_multipass),
            "extract_pgs": bool(extract_pgs),
            "add_external_text_subs": bool(add_external_text_subs),
            "ocr_engine": ocr_engine,
            "ocr_target_langs": ocr_target_langs,
            "pgsrip_bin": pgsrip_bin,
            "tessdata_prefix": tessdata_prefix,
            "encoding_profile": encoding_profile,
            "run_mode": bool(run_mode),
            "delete_bak_at_end": bool(delete_bak_at_end),
            "cleanup_only": bool(cleanup_only),
            "target_path": str(target_path) if target_path else "",
        }

    def persist_db(payload: Dict[str, Any]) -> None:
        try:
            db_path = report_dir / "mediashrinker_runs.sqlite"
            persist_run_to_db(
                payload=payload,
                report_json_path=json_path,
                report_log_path=log_path,
                analyses=analyses,
                mkvmerge_bin=mkvmerge,
                db_path=db_path,
            )
            log(f"[DB] Run salvato in {db_path}")
        except Exception as e:
            log(f"[WRN] DB persist failed: {e}")

    def _write_json_atomic(payload: Dict[str, Any]) -> None:
        tmp_path = json_path.with_suffix(json_path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            tmp_path.replace(json_path)
        except Exception as e:
            try:
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, ensure_ascii=False)
            except Exception:
                log(f"[WRN] live report write failed: {e}")

    def _compute_totals_snapshot(
        plan_items: List[PlanItem],
        result_items: List[JobResult],
        current_to_process_count: int,
    ) -> Dict[str, Any]:
        scanned_input = 0
        for a in analyses.values():
            try:
                scanned_input += int(a.size_bytes or 0)
            except Exception:
                pass

        queued_input = 0
        for p in plan_items:
            if not (bool(p.need_transcode) or bool(p.need_subfix)):
                continue
            a = analyses.get(p.path)
            if not a:
                continue
            try:
                queued_input += int(a.size_bytes or 0)
            except Exception:
                pass

        processed_input = 0
        processed_output = 0
        for r in result_items:
            if (r.action or "") not in ("transcoded", "subfixed"):
                continue
            if r.output_bytes is None:
                continue
            a = analyses.get(r.path)
            if not a:
                continue
            try:
                processed_input += int(a.size_bytes or 0)
                processed_output += int(r.output_bytes or 0)
            except Exception:
                pass

        delta = processed_output - processed_input
        delta_pct = (float(delta) * 100.0 / float(processed_input)) if processed_input > 0 else None
        return {
            "scanned_input_bytes": scanned_input,
            "queued_input_bytes": queued_input,
            "processed_input_bytes": processed_input,
            "processed_output_bytes": processed_output,
            "processed_delta_bytes": delta,
            "processed_delta_pct": delta_pct,
            "to_process_count": int(current_to_process_count),
            "results_done_count": int(len(result_items)),
        }

    def _build_payload_snapshot(
        *,
        mode_value: str,
        status: str,
        current_to_process_count: int,
        aborted: bool = False,
        aborted_phase: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "started": ts,
            "mode": mode_value,
            "status": status,
            "cfg_saved": save_config,
            "config": cfg_snapshot(),
            "run_wall_sec": (time.time() - run_t0),
            "plan": [asdict(x) for x in plan],
            "results": [asdict(r) for r in results],
            "totals": _compute_totals_snapshot(plan, results, current_to_process_count),
            "jobs_live": snapshot_live_slots(),
        }
        if aborted:
            payload["aborted"] = True
            if aborted_phase:
                payload["aborted_phase"] = aborted_phase
        return payload

    def write_report_snapshot(
        *,
        mode_value: str,
        status: str,
        current_to_process_count: int,
        persist: bool = False,
        aborted: bool = False,
        aborted_phase: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = _build_payload_snapshot(
            mode_value=mode_value,
            status=status,
            current_to_process_count=current_to_process_count,
            aborted=aborted,
            aborted_phase=aborted_phase,
        )
        _write_json_atomic(payload)
        if persist:
            persist_db(payload)
        return payload

    sigint_count = {"n": 0}
    def _handle_sigint(signum: int, frame: Any) -> None:
        sigint_count["n"] += 1
        if sigint_count["n"] == 1:
            STOP_REQUESTED.set()
            log("[STOP] CTRL+C ricevuto: arresto di tutti i job in corso...")
            terminate_active_procs()
        else:
            log("[STOP] Secondo CTRL+C: uscita forzata.")
            terminate_active_procs()
            raise SystemExit(130)

    signal.signal(signal.SIGINT, _handle_sigint)

    log(f"MediaShrinker start run={run_mode} delete_bak_at_end={delete_bak_at_end} cleanup_only={cleanup_only} yes={auto_yes}")
    log(f"roots={', '.join(str(r) for r in roots)}")
    log(f"staging_dir={staging_dir}")
    log(f"encoder={encoder} mode=size-efficient standards=movie/series multipass={not no_multipass}")
    log(f"jobs={jobs}")
    log(f"rules: bitrate>= {bitrate_threshold_mbps} Mbps, 4k_bitrate>= {bitrate_4k_mbps} Mbps")
    log(f"subtitle policy: Plex/space-first; keep original subtitles, never drop PGS/VobSub just for compatibility, OCR only during processed files when missing target text can be added. target_langs={sorted(TARGET_OCR_LANGS)}")
    log(f"extract_pgs={extract_pgs} add_external_text_subs={add_external_text_subs} ocr_engine={ocr_engine} ocr_target_langs={ocr_target_langs} pgsrip_bin={pgsrip_bin} tessdata_prefix={tessdata_prefix}")
    log("pgsrip policy: OCR language is dynamic per track (ita->it, eng->en). external text subtitles are muxed only when the file is already being processed.")
    if ocr_engine == "none":
        log("[OCR] disabled (pgsrip missing or explicitly disabled).")
    init_progress_slots(jobs if run_mode else 0)
    cleaned_staging_entries = cleanup_staging_dir(staging_dir)
    log(f"[CLEAN-STARTUP] Cleared staging_dir entries: {cleaned_staging_entries}")

    if cleanup_only:
        deleted = cleanup_baks(roots)
        log(f"[CLEAN-ONLY] Deleted {deleted} .bak file(s) under selected libraries.")
        payload = {
            "started": ts,
            "mode": "cleanup-only",
            "cfg_saved": save_config,
            "deleted_bak": deleted,
            "deleted_staging_entries": cleaned_staging_entries,
            "roots": [str(r) for r in roots],
            "config": cfg_snapshot(),
            "run_wall_sec": (time.time() - run_t0),
            "plan": [],
            "results": [],
        }
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        persist_db(payload)
        log(f"Cleanup complete. Report: {json_path}")
        if save_config:
            save_cfg(DEFAULT_CFG_PATH, cfg)
        return 0

    # Scan files
    all_files: List[Path] = []
    if target_path is not None:
        all_files.extend(scan_files(target_path))
        log(f"target_path={target_path}")
    else:
        for r in roots:
            all_files.extend(scan_files(r))
    log(f"Found {len(all_files)} video files.")

    # Resume: skip paths already successfully processed in a previous (aborted) run
    resume_run_id: Optional[int] = getattr(args, "resume_run_id", None)
    if resume_run_id is not None:
        db_path_for_resume = report_dir / "mediashrinker_runs.sqlite"
        skip_paths = get_completed_paths(db_path_for_resume, resume_run_id)
        if skip_paths:
            before_count = len(all_files)
            all_files = [p for p in all_files if str(p) not in skip_paths]
            log(f"[RESUME] Riprendo run #{resume_run_id}: esclusi {before_count - len(all_files)} file già elaborati, rimangono {len(all_files)}.")
        else:
            log(f"[RESUME] run #{resume_run_id}: nessun file completato trovato nel DB, procedo normalmente.")

    plan: List[PlanItem] = []
    results: List[JobResult] = []
    to_process: List[PlanItem] = []
    mode_for_snap = "run" if run_mode else "plan"
    write_report_snapshot(
        mode_value=mode_for_snap,
        status="running",
        current_to_process_count=0,
        persist=False,
    )

    # -------- PLAN --------
    if run_mode:
        # RUN precheck on NAS using the same policy:
        # copy to staging only files that really need work.
        for i, p in enumerate(all_files, start=1):
            if STOP_REQUESTED.is_set():
                log("[STOP] Interruzione richiesta durante RUN precheck: interrompo la scansione.")
                break
            try:
                log(f"[RUN-PRECHECK] analyzing [{i}/{len(all_files)}] {p}")
                a = analyze_one(ffprobe, p)
                a = apply_rules(a, bitrate_threshold_mbps=bitrate_threshold_mbps, bitrate_4k_mbps=bitrate_4k_mbps)
                analyses[str(p)] = a

                need_subfix = False
                subtitle_plan_dict: Optional[Dict[str, Any]] = None
                audit_dicts: List[Dict[str, Any]] = []
                ocr_task_dicts: List[Dict[str, Any]] = []
                external_langs: Set[str] = set()

                # Subtitle precheck is needed only when video doesn't already require transcode.
                # In Plex/space-first mode, subtitles alone do not queue a file unless a future
                # explicit additive subtitle mode enables that behavior.
                if not a.should_transcode:
                    inv = read_subtitle_inventory(mkvmerge, p, mkvextract=mkvextract)
                    external_langs = find_external_text_langs(p) if add_external_text_subs else set()
                    sp = build_sub_plan(
                        inv,
                        external_text_langs=external_langs,
                        force_extract_subs=getattr(args, "force_extract_subs", False),
                    )
                    need_subfix = sp.need_subfix
                    subtitle_plan_dict = {"drop_ids": sp.drop_ids, "keep_ids": sp.keep_ids}
                    audit_dicts = [asdict(x) for x in sp.audit]
                    ocr_task_dicts = [asdict(x) for x in sp.ocr_tasks]

                item = PlanItem(
                    path=str(p),
                    need_transcode=bool(a.should_transcode),
                    need_subfix=bool(need_subfix),
                    reasons_video=a.reasons[:],
                    subtitle_plan=subtitle_plan_dict,
                    sub_audit=audit_dicts,
                    external_text_langs=sorted(list(external_langs)),
                    ocr_tasks=ocr_task_dicts,
                )
                plan.append(item)

                if item.need_transcode or item.need_subfix:
                    to_process.append(item)
                    log(f"[RUN-PRECHECK] PROCESS: {p}")
                    if item.need_transcode:
                        for rr in item.reasons_video:
                            log(f"       - {rr}")
                else:
                    log(f"[RUN-PRECHECK] SKIP:    {p}")
                write_report_snapshot(
                    mode_value="run",
                    status="running",
                    current_to_process_count=len(to_process),
                    persist=False,
                )
            except Exception as e:
                # If precheck fails, still queue the file and decide locally after copy-in.
                item = PlanItem(
                    path=str(p),
                    need_transcode=True,
                    need_subfix=False,
                    reasons_video=[f"precheck failed on NAS: {e}"],
                    subtitle_plan=None,
                    sub_audit=[],
                    external_text_langs=[],
                    ocr_tasks=[],
                )
                plan.append(item)
                to_process.append(item)
                log(f"[WRN] RUN precheck failed for {p}: {e} -> queued for local analysis")
                write_report_snapshot(
                    mode_value="run",
                    status="running",
                    current_to_process_count=len(to_process),
                    persist=False,
                )
        if to_process:
            log(f"[RUN] queued {len(to_process)} file(s) after precheck.")
    else:
        for i, p in enumerate(all_files, start=1):
            if STOP_REQUESTED.is_set():
                log("[STOP] Interruzione richiesta durante PLAN: interrompo la scansione.")
                break
            try:
                log(f"[PLAN] analyzing [{i}/{len(all_files)}] {p}")
                a = analyze_one(ffprobe, p)
                a = apply_rules(a, bitrate_threshold_mbps=bitrate_threshold_mbps, bitrate_4k_mbps=bitrate_4k_mbps)
                analyses[str(p)] = a

                inv: Optional[SubtitleInventory] = None
                sp: Optional[SubPlan] = None
                external_langs: Set[str] = set()
                need_subfix = False
                subtitle_plan_dict: Optional[Dict[str, Any]] = None
                audit_dicts: List[Dict[str, Any]] = []
                ocr_task_dicts: List[Dict[str, Any]] = []

                inv = read_subtitle_inventory(mkvmerge, p, mkvextract=mkvextract)
                external_langs = find_external_text_langs(p) if add_external_text_subs else set()
                sp = build_sub_plan(
                    inv,
                    external_text_langs=external_langs,
                    force_extract_subs=getattr(args, "force_extract_subs", False),
                )
                need_subfix = sp.need_subfix
                subtitle_plan_dict = {"drop_ids": sp.drop_ids, "keep_ids": sp.keep_ids}
                audit_dicts = [asdict(x) for x in sp.audit]
                ocr_task_dicts = [asdict(x) for x in sp.ocr_tasks]

                item = PlanItem(
                    path=str(p),
                    need_transcode=a.should_transcode,
                    need_subfix=need_subfix,
                    reasons_video=a.reasons[:],
                    subtitle_plan=subtitle_plan_dict,
                    sub_audit=audit_dicts,
                    external_text_langs=sorted(list(external_langs)),
                    ocr_tasks=ocr_task_dicts,
                )
                plan.append(item)

                if item.need_transcode or item.need_subfix:
                    log(f"[PLAN] PROCESS: {p}")
                    if item.need_transcode:
                        log("       VIDEO:")
                        for rr in item.reasons_video:
                            log(f"         - {rr}")
                    if item.need_subfix:
                        log("       SUBS:")
                        if item.subtitle_plan:
                            log(f"         - drop subtitle ids: {item.subtitle_plan.get('drop_ids')}")
                            log(f"         - keep subtitle ids: {item.subtitle_plan.get('keep_ids')}")
                        if item.external_text_langs:
                            log(f"         - external text langs: {item.external_text_langs}")
                        if item.ocr_tasks:
                            log(f"         - OCR tasks: {item.ocr_tasks}")
                        for line in format_audit_lines([LangAudit(**d) for d in item.sub_audit]):
                            log(line)
                else:
                    log(f"[PLAN] SKIP:    {p}")
                write_report_snapshot(
                    mode_value="plan",
                    status="running",
                    current_to_process_count=0,
                    persist=False,
                )

            except Exception as e:
                results.append(JobResult(str(p), "failed", [], 0.0, None, None, f"analysis error: {e}"))
                log(f"[ERR] analysis failed for {p}: {e}")
                write_report_snapshot(
                    mode_value="plan",
                    status="running",
                    current_to_process_count=0,
                    persist=False,
                )
                if STOP_REQUESTED.is_set():
                    log("[STOP] Interruzione richiesta: termino il PLAN.")
                    break

        to_process = [x for x in plan if x.need_transcode or x.need_subfix]

    if STOP_REQUESTED.is_set():
        payload = write_report_snapshot(
            mode_value="aborted",
            status="aborted",
            current_to_process_count=len(to_process),
            persist=True,
            aborted=True,
            aborted_phase="plan",
        )
        log(f"Run aborted during plan. Partial report: {json_path}")
        if save_config:
            save_cfg(DEFAULT_CFG_PATH, cfg)
        return 130

    if not run_mode:
        if delete_bak_at_end:
            deleted = cleanup_baks(roots)
            log(f"[CLEAN] Deleted {deleted} .bak file(s) under selected libraries.")
        payload = write_report_snapshot(
            mode_value="plan",
            status="completed",
            current_to_process_count=len(to_process),
            persist=True,
        )
        log(f"Plan complete. Report: {json_path}")
        if save_config:
            save_cfg(DEFAULT_CFG_PATH, cfg)
        return 0

    # RUN
    if not to_process:
        log("Nothing to process (no transcode needed under the Plex/space-first policy).")
        if delete_bak_at_end:
            log("[CLEAN] No successful transcode/swap in this run: no .bak deleted.")
        payload = write_report_snapshot(
            mode_value="run",
            status="completed",
            current_to_process_count=0,
            persist=True,
        )
        log(f"Run complete. Report: {json_path}")
        if save_config:
            save_cfg(DEFAULT_CFG_PATH, cfg)
        return 0

    if STOP_REQUESTED.is_set():
        log("[STOP] Interruzione richiesta: non avvio RUN.")
        if save_config:
            save_cfg(DEFAULT_CFG_PATH, cfg)
        return 130

    if not auto_yes:
        if not prompt_yes_no(
            f"Confermi elaborazione di {len(to_process)} file "
            "(staging su SSD solo per file processati, OCR/add subs se utile, transcode se serve, swap atomico)?",
            default_yes=True,
        ):
            log("Aborted by user.")
            if save_config:
                save_cfg(DEFAULT_CFG_PATH, cfg)
            return 2

    slot_queue: Optional[queue.Queue] = None
    if jobs > 1:
        slot_queue = queue.Queue()
        for s in range(1, jobs + 1):
            slot_queue.put(s)
    deleted_bak_immediate = {"n": 0}
    deleted_bak_lock = threading.Lock()

    def delete_bak_after_success(nas_src: Path) -> None:
        if not delete_bak_at_end:
            return
        bak = nas_src.with_suffix(nas_src.suffix + ".bak")
        try:
            if bak.exists():
                bak.unlink()
                with deleted_bak_lock:
                    deleted_bak_immediate["n"] += 1
                log(f"[CLEAN] deleted backup after successful transcode/swap: {bak}")
        except Exception as e:
            log(f"[WRN] failed deleting backup {bak}: {e}")

    def process_one(idx: int, it: PlanItem) -> JobResult:
        if STOP_REQUESTED.is_set():
            return JobResult(str(it.path), "aborted", it.reasons_video, 0.0, None, None, "stopped by user")
        slot = None
        if slot_queue is not None:
            slot = int(slot_queue.get())
            set_thread_progress_slot(slot)
        nas_src = Path(it.path)
        display_name = short_label(nas_src.name, 56)

        def set_slot_status(stage: str) -> None:
            if slot is None or PROGRESS_SLOTS <= 1:
                return
            _update_progress_slot(slot, f"[J{slot}] [{idx}/{len(to_process)}] {stage} {display_name}")

        t0 = time.time()
        job_dir = staging_dir / f"{safe_name(nas_src.stem)}.{idx}.{int(t0)}"
        job_dir.mkdir(parents=True, exist_ok=True)
        local_src = job_dir / nas_src.name
        try:
            if STOP_REQUESTED.is_set():
                return JobResult(str(nas_src), "aborted", it.reasons_video, 0.0, None, None, "stopped by user")
            set_slot_status("START")
            log(f"[{idx}/{len(to_process)}] START {nas_src}")
            log(f"      staging={job_dir}")
            log("      copy-in: NAS -> SSD")
            copy_in_prefix = f"[{idx}/{len(to_process)}] Copy SSD {short_label(nas_src.name, 48)}"
            copy_with_progress(nas_src, local_src, prefix=copy_in_prefix)
            if STOP_REQUESTED.is_set():
                set_slot_status("STOP requested")
                return JobResult(str(nas_src), "aborted", it.reasons_video, time.time() - t0, None, None, "stopped by user")

            set_slot_status("Analyze video")
            a_for_src = analyze_one(ffprobe, local_src)
            a_for_src = apply_rules(
                a_for_src,
                bitrate_threshold_mbps=bitrate_threshold_mbps,
                bitrate_4k_mbps=bitrate_4k_mbps,
            )
            analyses[str(nas_src)] = a_for_src

            set_slot_status("Analyze subs")
            sub_src = local_src
            if local_src.suffix.lower() != ".mkv":
                sub_src = job_dir / f"{nas_src.stem}.subsrc.mkv"
                log(f"      remux-source: convert non-MKV to MKV for reliable subtitle handling -> {sub_src.name}")
                mkvmerge_build_sub_source_mkv(mkvmerge, src=local_src, out_mkv=sub_src)

            inv_local = read_subtitle_inventory(mkvmerge, sub_src, mkvextract=mkvextract)
            if extract_pgs and ocr_engine == "pgsrip" and (Path(pgsrip_bin).exists() or shutil.which(pgsrip_bin) is not None):
                for tr in inv_local.non_text:
                    if tr.lang != "und":
                        continue
                    guessed_lang = infer_non_text_lang_via_probe_ocr(
                        mkvextract=mkvextract,
                        mkv_path=sub_src,
                        tr=tr,
                        pgsrip_bin=pgsrip_bin,
                        tessdata_prefix=tessdata_prefix,
                    )
                    if guessed_lang:
                        tr.lang = guessed_lang
                        log(f"      [LANG] inferred non-text track t{tr.id:02d} -> {guessed_lang} (probe OCR)")

            ext_langs = find_external_text_langs(nas_src) if add_external_text_subs else set()
            subplan = build_sub_plan(
                inv_local,
                external_text_langs=ext_langs,
                force_extract_subs=getattr(args, "force_extract_subs", False),
            )
            it.need_transcode = bool(a_for_src.should_transcode)
            it.need_subfix = bool(subplan.need_subfix)
            it.reasons_video = a_for_src.reasons[:]
            it.subtitle_plan = {"drop_ids": subplan.drop_ids, "keep_ids": subplan.keep_ids}
            it.sub_audit = [asdict(x) for x in subplan.audit]
            it.external_text_langs = sorted(list(ext_langs))
            it.ocr_tasks = [asdict(x) for x in subplan.ocr_tasks]

            log(f"      SUBTITLE normalize drop_ids={subplan.drop_ids} keep_ids={subplan.keep_ids}")
            log(f"      SUBS summary: text={len(inv_local.text)} non_text={len(inv_local.non_text)} ocr_tasks={len(subplan.ocr_tasks)}")
            if subplan.external_text_langs:
                log(f"      ext_text_langs={subplan.external_text_langs}")
            for line in format_audit_lines(subplan.audit):
                log(line)

            add_srt: List[Tuple[Path, str, str, bool, bool]] = []
            if add_external_text_subs:
                external_tracks = find_external_text_subtitles(nas_src)
                if external_tracks:
                    add_srt.extend(external_tracks)
                    log(f"      external text subtitles queued for mux: {len(external_tracks)}")
            if extract_pgs and ocr_engine == "pgsrip" and subplan.ocr_tasks:
                if not Path(pgsrip_bin).exists() and shutil.which(pgsrip_bin) is None:
                    set_slot_status("FAILED (pgsrip missing)")
                    raise RuntimeError(f"pgsrip not found: {pgsrip_bin}")
                for task in subplan.ocr_tasks:
                    tr = next((x for x in inv_local.non_text if x.id == task.track_id), None)
                    if not tr:
                        log(f"[WRN] OCR task track not found id={task.track_id} (skip)")
                        continue
                    try:
                        set_slot_status(f"OCR t{task.track_id:02d} {task.lang}")
                        sup = extract_pgs_for_pgsrip(mkvextract, sub_src, tr, job_dir)
                        lang_ietf = mkv_lang_to_ietf(task.lang)
                        log(f"[OCR] pgsrip sup={sup.name} codec={task.codec} lang={task.lang} forced={task.forced}")
                        srt = pgsrip_sup_to_srt(
                            sup,
                            pgsrip_bin=pgsrip_bin,
                            tessdata_prefix=tessdata_prefix,
                            ocr_langs_ietf=[lang_ietf],
                        )
                        nm0 = (tr.name or "").strip() or "OCR"
                        nm = f"{nm0} (Forced OCR)" if task.forced else f"{nm0} (OCR)"
                        add_srt.append((srt, task.lang, nm, bool(task.forced), False))
                    except Exception as ocr_err:
                        log(f"[WRN] OCR failed track={task.track_id} lang={task.lang}: {ocr_err} (continuo senza questa lingua)")
            elif (not extract_pgs) and subplan.ocr_tasks:
                log("[WRN] OCR tasks exist but extract_pgs=false -> skipping extract+OCR.")
            elif ocr_engine == "none" and subplan.ocr_tasks:
                log("[WRN] OCR tasks exist but ocr_engine=none -> skipping OCR.")
            else:
                log("      OCR: no extraction needed.")

            do_transcode = bool(a_for_src.should_transcode)
            if do_transcode:
                set_slot_status("Transcode")
                hb_out = job_dir / f"{nas_src.stem}.enc.mkv"
                final_out = job_dir / f"{nas_src.stem}.final.mkv"
                hb_log = report_dir / f"ffmpeg-{safe_name(nas_src.stem)}-{idx}-{int(t0)}.log"
                kind = content_kind_for_path(nas_src, movies_root, series_root)
                active_profile = get_profile(encoding_profile)
                selected_cq = pick_transcode_cq(a_for_src, kind, profile=active_profile)
                cap_kbps, cap_from_source = pick_transcode_vb_kbps(a_for_src, kind, profile=active_profile)
                enc_preset = (
                    active_profile.get("preset_nvenc", "p5") if encoder == "hevc_nvenc"
                    else active_profile.get("preset_x265", "medium")
                )
                cmd = ffmpeg_cmd(
                    ffmpeg_bin=ffmpeg_bin,
                    src=local_src,
                    dst_local=hb_out,
                    encoder=encoder,
                    cq=selected_cq,
                    preset=enc_preset,
                    multipass=(not no_multipass),
                    maxrate_kbps=cap_kbps,
                )
                prefix = f"[{idx}/{len(to_process)}] {nas_src.name}"
                log(f"[RUN] TRANSCODE {nas_src}")
                log(f"      hb_out: {hb_out}")
                log(f"      cq: size-efficient ({kind}) -> CQ {selected_cq}")
                if cap_from_source:
                    log(f"      bitrate cap: source-aware ceiling -> {cap_kbps} kbps")
                else:
                    log(f"      bitrate cap: standard ceiling -> {cap_kbps} kbps")
                log(f"      cmd: ffmpeg ... (full command in {hb_log})")
                rc, elapsed_enc = run_ffmpeg_with_progress_tty(
                    cmd,
                    prefix=prefix,
                    log_path=hb_log,
                    duration_sec=(a_for_src.duration_sec if a_for_src else None),
                )
                if STOP_REQUESTED.is_set():
                    set_slot_status("ABORTED")
                    log(f"[STOP] transcode aborted by user for {nas_src}")
                    return JobResult(str(nas_src), "aborted", it.reasons_video, elapsed_enc, rc, None, "stopped by user")
                if rc != 0:
                    set_slot_status(f"FAILED (ffmpeg rc={rc})")
                    log(f"[BAD] rc={rc} {nas_src} (see {hb_log})")
                    return JobResult(str(nas_src), "failed", it.reasons_video, elapsed_enc, rc, None, f"ffmpeg failed rc={rc} (see {hb_log})")
                try:
                    set_slot_status("Remux final")
                    mkvmerge_build_final_from_encoded(
                        mkvmerge,
                        encoded_in=hb_out,
                        original_local=sub_src,
                        out_final=final_out,
                        keep_sub_ids_from_original=subplan.keep_ids,
                        add_srt=add_srt,
                    )
                except Exception as e:
                    if is_matroska_structure_error(str(e)):
                        log(f"[WRN] final remux failed due to source Matroska corruption; fallback to encoded-only output for {nas_src}")
                        log("      fallback note: source subtitle tracks will not be preserved in this file.")
                        copy_out_prefix = f"[{idx}/{len(to_process)}] Copy NAS {short_label(nas_src.name, 48)}"
                        try:
                            src_size = int(local_src.stat().st_size)
                            out_size = int(hb_out.stat().st_size)
                            if src_size > 0 and out_size > int(src_size * GROWTH_GUARD_MAX_RATIO):
                                set_slot_status("SKIP (growth guard)")
                                total_elapsed = time.time() - t0
                                growth_pct = ((out_size - src_size) * 100.0 / src_size)
                                reason = (
                                    f"growth guard: encoded-only fallback larger than source "
                                    f"({out_size} > {src_size}, {growth_pct:+.1f}%)"
                                )
                                log(f"[SKIP] {reason}; keeping original on NAS for {nas_src}")
                                return JobResult(str(nas_src), "skipped-growth-guard", it.reasons_video + [reason], total_elapsed, rc, None, None)
                        except Exception as gg_err:
                            log(f"[WRN] growth-guard check failed (continuo): {gg_err}")
                        set_slot_status("Fallback copy NAS")
                        out_bytes, swap_err = atomic_swap_on_nas(nas_src, hb_out, copy_prefix=copy_out_prefix)
                        total_elapsed = time.time() - t0
                        if swap_err:
                            set_slot_status("FAILED (fallback swap)")
                            log(f"[BAD] fallback swap failed {nas_src}: {swap_err}")
                            return JobResult(str(nas_src), "failed", it.reasons_video, total_elapsed, rc, out_bytes, f"fallback swap failed: {swap_err}")
                        delete_bak_after_success(nas_src)
                        set_slot_status("DONE (fallback)")
                        fb_reason = "fallback: source matroska corrupt, kept encoded-only output (no source subtitles)"
                        log(f"[OK]  TRANSCODED (fallback) {nas_src} in {total_elapsed:.1f}s")
                        return JobResult(str(nas_src), "transcoded", it.reasons_video + [fb_reason], total_elapsed, rc, out_bytes, None)
                    set_slot_status("FAILED (final remux)")
                    log(f"[BAD] final remux failed {nas_src}: {e}")
                    return JobResult(str(nas_src), "failed", it.reasons_video, time.time() - t0, rc, None, f"final remux failed: {e}")
                copy_out_prefix = f"[{idx}/{len(to_process)}] Copy NAS {short_label(nas_src.name, 48)}"
                # Growth guard: avoid replacing source with larger file when size-efficiency is the goal.
                try:
                    src_size = int(local_src.stat().st_size)
                    out_size = int(final_out.stat().st_size)
                    if src_size > 0 and out_size > int(src_size * GROWTH_GUARD_MAX_RATIO):
                        set_slot_status("SKIP (growth guard)")
                        total_elapsed = time.time() - t0
                        growth_pct = ((out_size - src_size) * 100.0 / src_size)
                        reason = (
                            f"growth guard: encoded output larger than source "
                            f"({out_size} > {src_size}, {growth_pct:+.1f}%)"
                        )
                        log(f"[SKIP] {reason}; keeping original on NAS for {nas_src}")
                        return JobResult(str(nas_src), "skipped-growth-guard", it.reasons_video + [reason], total_elapsed, rc, None, None)
                except Exception as gg_err:
                    log(f"[WRN] growth-guard check failed (continuo): {gg_err}")
                set_slot_status("Copy NAS")
                out_bytes, swap_err = atomic_swap_on_nas(nas_src, final_out, copy_prefix=copy_out_prefix)
                total_elapsed = time.time() - t0
                if swap_err:
                    set_slot_status("FAILED (swap)")
                    log(f"[BAD] swap failed {nas_src}: {swap_err}")
                    return JobResult(str(nas_src), "failed", it.reasons_video, total_elapsed, rc, out_bytes, f"swap failed: {swap_err}")
                delete_bak_after_success(nas_src)
                set_slot_status("DONE")
                log(f"[OK]  TRANSCODED {nas_src} in {total_elapsed:.1f}s")
                return JobResult(str(nas_src), "transcoded", it.reasons_video, total_elapsed, rc, out_bytes, None)

            if not it.need_subfix:
                total_elapsed = time.time() - t0
                set_slot_status("SKIP (video unchanged)")
                log(f"[SKIP] no transcode/subfix needed after local analysis: {nas_src}")
                return JobResult(str(nas_src), "skipped", it.reasons_video, total_elapsed, None, None, None)

            final_out = job_dir / f"{nas_src.stem}.subfix.final.mkv"
            set_slot_status("SUBFIX remux")
            log(f"[RUN] SUBFIX {nas_src}")
            try:
                mkvmerge_build_final_from_source(
                    mkvmerge,
                    local_src=sub_src,
                    out_final=final_out,
                    keep_sub_ids=subplan.keep_ids,
                    add_srt=add_srt,
                )
            except Exception as e:
                set_slot_status("FAILED (subfix)")
                log(f"[BAD] subfix failed {nas_src}: {e}")
                return JobResult(str(nas_src), "failed", [], time.time() - t0, None, None, f"subfix mkvmerge failed: {e}")
            copy_out_prefix = f"[{idx}/{len(to_process)}] Copy NAS {short_label(nas_src.name, 48)}"
            set_slot_status("Copy NAS")
            out_bytes, swap_err = atomic_swap_on_nas(nas_src, final_out, copy_prefix=copy_out_prefix)
            total_elapsed = time.time() - t0
            if swap_err:
                set_slot_status("FAILED (swap)")
                log(f"[BAD] swap failed {nas_src}: {swap_err}")
                return JobResult(str(nas_src), "failed", [], total_elapsed, None, out_bytes, f"swap failed: {swap_err}")
            set_slot_status("DONE")
            log(f"[OK]  SUBFIXED {nas_src} in {total_elapsed:.1f}s")
            return JobResult(str(nas_src), "subfixed", [], total_elapsed, None, out_bytes, None)
        except Exception as e:
            total_elapsed = time.time() - t0
            set_slot_status("FAILED")
            log(f"[BAD] {nas_src} failed: {e}")
            return JobResult(str(nas_src), "failed", [], total_elapsed, None, None, str(e))
        finally:
            if slot is not None:
                _clear_progress_slot(slot)
                if slot_queue is not None:
                    slot_queue.put(slot)
            set_thread_progress_slot(None)
            try:
                shutil.rmtree(job_dir, ignore_errors=True)
            except Exception:
                pass

    indexed_items = list(enumerate(to_process, start=1))
    live_tick_stop = threading.Event()
    live_tick_thread: Optional[threading.Thread] = None
    RUN_ACTIVE = True
    try:
        def _live_tick() -> None:
            while not live_tick_stop.is_set() and not STOP_REQUESTED.is_set():
                try:
                    write_report_snapshot(
                        mode_value="run",
                        status="running",
                        current_to_process_count=len(to_process),
                        persist=False,
                    )
                except Exception:
                    pass
                live_tick_stop.wait(2.0)

        live_tick_thread = threading.Thread(target=_live_tick, name="mediashrinker-live-tick", daemon=True)
        live_tick_thread.start()

        if jobs == 1:
            for idx, it in indexed_items:
                if STOP_REQUESTED.is_set():
                    break
                results.append(process_one(idx, it))
                write_report_snapshot(
                    mode_value="run",
                    status="running",
                    current_to_process_count=len(to_process),
                    persist=False,
                )
        else:
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
            futs: List[concurrent.futures.Future] = []
            collected: Set[concurrent.futures.Future] = set()
            try:
                futs = [ex.submit(process_one, idx, it) for idx, it in indexed_items]
                pending: Set[concurrent.futures.Future] = set(futs)
                while pending:
                    done, pending = concurrent.futures.wait(
                        pending,
                        timeout=0.3,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for fut in done:
                        collected.add(fut)
                        if fut.cancelled():
                            continue
                        try:
                            results.append(fut.result())
                        except Exception as e:
                            results.append(JobResult("", "failed", [], 0.0, None, None, str(e)))
                        write_report_snapshot(
                            mode_value="run",
                            status="running",
                            current_to_process_count=len(to_process),
                            persist=False,
                        )
                    if STOP_REQUESTED.is_set():
                        log("[STOP] Stop richiesto: annullo i job pendenti...")
                        terminate_active_procs()
                        for fut in pending:
                            fut.cancel()
                        break
            except KeyboardInterrupt:
                STOP_REQUESTED.set()
                log("[STOP] Interruzione utente: annullo i job pendenti...")
                terminate_active_procs()
                for fut in futs:
                    fut.cancel()
            finally:
                for fut in futs:
                    if fut in collected:
                        continue
                    if fut.done() and not fut.cancelled():
                        try:
                            results.append(fut.result())
                        except Exception:
                            pass
                ex.shutdown(wait=False, cancel_futures=True)
    finally:
        live_tick_stop.set()
        if live_tick_thread is not None:
            try:
                live_tick_thread.join(timeout=1.0)
            except Exception:
                pass
        RUN_ACTIVE = False
        if PARALLEL_FIXED_ROWS:
            with CONSOLE_LOCK:
                print()

    if delete_bak_at_end:
        log(f"[CLEAN] Immediate backup deletion complete. Deleted {deleted_bak_immediate['n']} .bak file(s).")

    if STOP_REQUESTED.is_set():
        payload = write_report_snapshot(
            mode_value="aborted",
            status="aborted",
            current_to_process_count=len(to_process),
            persist=True,
            aborted=True,
            aborted_phase="run",
        )
        log(f"Run aborted during run. Partial report: {json_path}")
        if save_config:
            save_cfg(DEFAULT_CFG_PATH, cfg)
        return 130

    payload = write_report_snapshot(
        mode_value="run",
        status="completed",
        current_to_process_count=len(to_process),
        persist=True,
    )
    log(f"Run complete. Report: {json_path}")

    if notify_url:
        totals = (payload.get("totals") or {})
        done = totals.get("results_done_count", 0)
        transcoded = totals.get("results_transcoded_count", 0)
        failed = totals.get("results_failed_count", 0)
        delta_gib = float(totals.get("processed_delta_bytes") or 0) / (1024 ** 3)
        msg = (
            f"Run completato: {done} file processati, {transcoded} transcodificati"
            + (f", {failed} errori" if failed else "")
            + f", {delta_gib:+.2f} GiB"
        )
        send_ntfy(notify_url, "MediaShrinker ✓", msg)

    if save_config:
        save_cfg(DEFAULT_CFG_PATH, cfg)

    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        try:
            print("[STOP] Interruzione utente (CTRL+C).")
        except Exception:
            pass
        raise SystemExit(130)
