from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Analysis:
    path: str
    size_bytes: int
    container: str
    v_codec: Optional[str]
    v_bitrate_bps: Optional[int]
    v_width: Optional[int]
    v_height: Optional[int]
    dv_profile: Optional[int]
    dv_el_present: Optional[int]
    a_codecs: List[str]
    should_transcode: bool
    reasons: List[str]
    duration_sec: Optional[float] = None
