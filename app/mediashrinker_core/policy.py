from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .models import Analysis


KEEP_SOURCE_BITRATE_MAX_KBPS = 40_000

# ---------------------------------------------------------------------------
# Profili di encoding nominali
# ---------------------------------------------------------------------------
# cq_*      → NVENC CQ o libx265 CRF  (più basso = qualità maggiore / file più grande)
# kbps_*    → cap bitrate in kbps      (usato come maxrate per NVENC, -b:v per VAAPI/x265)
# preset_nvenc → p1 (lento/migliore compressione) … p7 (veloce/peggiore compressione)
# preset_x265  → veryslow … ultrafast
# preset_vaapi → non usato direttamente ma conservato per documentazione

ENCODING_PROFILES: Dict[str, Dict[str, Any]] = {
    "space_saver": {
        "cq_movies":   {"4k": 26, "1080p": 28, "720p": 29, "sd": 30},
        "cq_series":   {"4k": 27, "1080p": 30, "720p": 31, "sd": 32},
        "kbps_movies": {"4k": 8000,  "1080p": 4500, "720p": 2800, "sd": 1500},
        "kbps_series": {"4k": 6000,  "1080p": 3200, "720p": 2000, "sd": 1100},
        "preset_nvenc": "p7",
        "preset_x265":  "slow",
    },
    "balanced": {
        "cq_movies":   {"4k": 22, "1080p": 24, "720p": 25, "sd": 26},
        "cq_series":   {"4k": 23, "1080p": 26, "720p": 27, "sd": 28},
        "kbps_movies": {"4k": 12000, "1080p": 7000, "720p": 4500, "sd": 2200},
        "kbps_series": {"4k":  9000, "1080p": 5000, "720p": 3200, "sd": 1800},
        "preset_nvenc": "p5",
        "preset_x265":  "medium",
    },
    "quality": {
        "cq_movies":   {"4k": 20, "1080p": 22, "720p": 23, "sd": 24},
        "cq_series":   {"4k": 21, "1080p": 23, "720p": 24, "sd": 25},
        "kbps_movies": {"4k": 16000, "1080p": 9000,  "720p": 6000, "sd": 3000},
        "kbps_series": {"4k": 12000, "1080p": 7000,  "720p": 4500, "sd": 2200},
        "preset_nvenc": "p4",
        "preset_x265":  "slow",
    },
    "hq": {
        "cq_movies":   {"4k": 18, "1080p": 20, "720p": 21, "sd": 22},
        "cq_series":   {"4k": 19, "1080p": 21, "720p": 22, "sd": 23},
        "kbps_movies": {"4k": 20000, "1080p": 12000, "720p": 8000, "sd": 4000},
        "kbps_series": {"4k": 16000, "1080p":  9000, "720p": 6000, "sd": 3000},
        "preset_nvenc": "p3",
        "preset_x265":  "slow",
    },
}

DEFAULT_PROFILE = "balanced"


def get_profile(name: Optional[str]) -> Dict[str, Any]:
    """Restituisce il profilo per nome; cade su 'balanced' se non trovato."""
    return ENCODING_PROFILES.get((name or DEFAULT_PROFILE).lower(), ENCODING_PROFILES[DEFAULT_PROFILE])


def _res_key(a: Optional[Analysis]) -> str:
    w = int(a.v_width or 0) if a else 0
    h = int(a.v_height or 0) if a else 0
    px = w * h
    if px >= 3800 * 2100:
        return "4k"
    if px >= 1920 * 1000:
        return "1080p"
    if px >= 1280 * 700:
        return "720p"
    return "sd"


def apply_rules(a: Analysis, *, bitrate_threshold_mbps: float, bitrate_4k_mbps: float) -> Analysis:
    reasons = []
    v_codec = (a.v_codec or "").strip().lower()
    is_hevc = (v_codec == "hevc")

    if not is_hevc:
        reasons.append(f"Video codec non-HEVC ({a.v_codec or 'unknown'})")

    if a.dv_profile == 7:
        reasons.append("Dolby Vision Profile 7 (compatibilità stick)")
    if a.dv_el_present == 1:
        reasons.append("Dolby Vision EL present (DOVIWithEL)")

    if (not is_hevc) and (a.v_bitrate_bps is not None):
        mbps = a.v_bitrate_bps / 1_000_000
        if mbps >= bitrate_threshold_mbps:
            reasons.append(f"High bitrate (~{mbps:.1f} Mbps >= {bitrate_threshold_mbps})")
        if a.v_width and a.v_height and a.v_width >= 3800 and a.v_height >= 2100 and mbps >= bitrate_4k_mbps:
            reasons.append(f"4K + high bitrate (~{mbps:.1f} Mbps >= {bitrate_4k_mbps})")

    a.reasons = reasons
    a.should_transcode = len(reasons) > 0
    return a


def pick_transcode_vb_kbps(
    a: Optional[Analysis],
    content_kind: str,
    profile: Optional[Dict[str, Any]] = None,
) -> Tuple[int, bool]:
    """Returns (vb_kbps, computed_from_source)."""
    p = profile or get_profile(DEFAULT_PROFILE)
    std = p["kbps_series"] if content_kind == "series" else p["kbps_movies"]
    base = std[_res_key(a)]

    if a and a.v_bitrate_bps is not None:
        src_kbps = max(1000, int(round(a.v_bitrate_bps / 1000.0)))
        target = min(base, max(1000, int(src_kbps * 0.85)))
        return max(1000, target), True

    return max(1000, base), False


def pick_transcode_cq(
    a: Optional[Analysis],
    content_kind: str,
    profile: Optional[Dict[str, Any]] = None,
) -> int:
    """Returns CQ/CRF target. Lower = higher quality / bigger file."""
    p = profile or get_profile(DEFAULT_PROFILE)
    std = p["cq_series"] if content_kind == "series" else p["cq_movies"]
    return int(std[_res_key(a)])


def content_kind_for_path(path: Path, movies_root: Path, series_root: Path) -> str:
    p = str(path)
    s = str(series_root)
    m = str(movies_root)
    if p == s or p.startswith(s + os.sep):
        return "series"
    if p == m or p.startswith(m + os.sep):
        return "movie"
    return "movie"
