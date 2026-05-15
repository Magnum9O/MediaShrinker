from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import Analysis

DEFAULT_DB_PATH = Path("/reports") / "mediashrinker_runs.sqlite"
TEXT_CODEC_HINTS = (
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
)


def _run_cmd_capture(cmd: Sequence[str]) -> Tuple[int, str, str]:
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out, err = p.communicate()
    return int(p.returncode or 0), out or "", err or ""


def _is_text_sub_codec(codec: str) -> bool:
    c = (codec or "").lower()
    return any(x in c for x in TEXT_CODEC_HINTS)


def _json_dumps(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False)


def _mkvmerge_sub_tracks(mkvmerge_bin: str, media_path: Path) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not media_path.exists():
        return [], "missing file"
    rc, out, err = _run_cmd_capture([mkvmerge_bin, "-J", str(media_path)])
    if rc != 0:
        return [], f"mkvmerge -J failed: {err.strip()}"
    try:
        data = json.loads(out)
    except Exception as ex:
        return [], f"json parse failed: {ex}"

    tracks: List[Dict[str, Any]] = []
    for t in (data.get("tracks") or []):
        if t.get("type") != "subtitles":
            continue
        props = t.get("properties") or {}
        codec = t.get("codec") or ""
        tracks.append(
            {
                "track_id": int(t.get("id")),
                "codec": codec,
                "lang": (props.get("language") or "und"),
                "name": (props.get("track_name") or ""),
                "forced": 1 if bool(props.get("forced_track") or False) else 0,
                "default_track": 1 if bool(props.get("default_track") or False) else 0,
                "is_text": 1 if _is_text_sub_codec(codec) else 0,
            }
        )
    return tracks, None


def _uniq_csv(items: List[str]) -> str:
    vals = sorted({(x or "und").strip() or "und" for x in items})
    return ",".join(vals)


def _langs_csv(tracks: List[Dict[str, Any]], *, is_text: bool) -> str:
    return _uniq_csv([str(t.get("lang") or "und") for t in tracks if bool(t.get("is_text")) == is_text])


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started TEXT,
            finished_at TEXT NOT NULL,
            mode TEXT,
            status TEXT NOT NULL DEFAULT 'completed',
            report_json_path TEXT NOT NULL,
            report_log_path TEXT,
            plan_count INTEGER NOT NULL DEFAULT 0,
            to_process_count INTEGER NOT NULL DEFAULT 0,
            results_count INTEGER NOT NULL DEFAULT 0,
            run_wall_sec REAL,
            total_elapsed_sec REAL,
            total_output_bytes INTEGER,
            config_json TEXT
        );

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            path TEXT NOT NULL,
            action TEXT,
            need_transcode INTEGER NOT NULL DEFAULT 0,
            need_subfix INTEGER NOT NULL DEFAULT 0,
            reasons_video_json TEXT,
            subtitle_plan_json TEXT,
            sub_audit_json TEXT,
            ocr_tasks_json TEXT,
            external_text_langs_json TEXT,
            elapsed_sec REAL,
            output_bytes INTEGER,
            hb_exit_code INTEGER,
            error TEXT,
            size_before_bytes INTEGER,
            size_after_bytes INTEGER,
            size_delta_bytes INTEGER,
            size_delta_pct REAL,
            subs_before_text_langs TEXT,
            subs_before_nontext_langs TEXT,
            subs_after_text_langs TEXT,
            subs_after_nontext_langs TEXT,
            ocr_planned_langs TEXT,
            ocr_after_langs TEXT,
            v_codec TEXT,
            v_bitrate_bps INTEGER,
            v_width INTEGER,
            v_height INTEGER,
            duration_sec REAL,
            container TEXT,
            source_size_bytes INTEGER,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_files_run_id ON files(run_id);
        CREATE INDEX IF NOT EXISTS idx_files_run_path ON files(run_id, path);

        CREATE TABLE IF NOT EXISTS subtitle_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            stage TEXT NOT NULL,
            track_id INTEGER NOT NULL,
            codec TEXT,
            lang TEXT,
            name TEXT,
            forced INTEGER NOT NULL DEFAULT 0,
            default_track INTEGER NOT NULL DEFAULT 0,
            is_text INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS schedules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL DEFAULT '',
            cron_expr TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'plan',
            library TEXT NOT NULL DEFAULT 'both',
            enabled INTEGER NOT NULL DEFAULT 1,
            last_run_at TEXT,
            last_run_id INTEGER,
            next_run_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(last_run_id) REFERENCES runs(id) ON DELETE SET NULL
        );
        """
    )
    sub_cols = _table_cols(conn, "subtitle_tracks")
    if sub_cols:
        file_col = "file_path" if "file_path" in sub_cols else ("path" if "path" in sub_cols else None)
        if file_col:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_sub_tracks_run_file_stage ON subtitle_tracks(run_id, {file_col}, stage)"
            )
    run_cols = _table_cols(conn, "runs")
    if run_cols and "status" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'")
    conn.commit()


def persist_run_to_db(
    *,
    payload: Dict[str, Any],
    report_json_path: Path,
    report_log_path: Optional[Path],
    analyses: Dict[str, Analysis],
    mkvmerge_bin: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    last_err: Optional[Exception] = None
    for attempt in range(1, 9):
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            ensure_schema(conn)
            run_cols = _table_cols(conn, "runs")
            sub_cols = _table_cols(conn, "subtitle_tracks")
            plan = payload.get("plan") or []
            results = payload.get("results") or []
            to_process = [x for x in plan if bool(x.get("need_transcode")) or bool(x.get("need_subfix"))]
            total_elapsed = float(sum(float((r.get("elapsed_sec") or 0.0)) for r in results))
            total_output = int(sum(int(r.get("output_bytes") or 0) for r in results))
            finished_at = datetime.now(timezone.utc).isoformat()
            config_json = _json_dumps(payload.get("config") or {})

            run_data: Dict[str, Any] = {
                "started": payload.get("started"),
                "mode": payload.get("mode"),
                "status": payload.get("status", "completed"),
                "plan_count": len(plan),
                "to_process_count": len(to_process),
                "results_count": len(results),
                "total_elapsed_sec": total_elapsed,
                "total_output_bytes": total_output,
            }
            if "finished_at" in run_cols:
                run_data["finished_at"] = finished_at
            if "generated_at" in run_cols:
                run_data["generated_at"] = finished_at
            if "report_json_path" in run_cols:
                run_data["report_json_path"] = str(report_json_path)
            if "report_path" in run_cols:
                run_data["report_path"] = str(report_json_path)
            if "report_log_path" in run_cols:
                run_data["report_log_path"] = str(report_log_path) if report_log_path else None
            if "run_wall_sec" in run_cols:
                run_data["run_wall_sec"] = float(payload.get("run_wall_sec") or 0.0)
            if "config_json" in run_cols:
                run_data["config_json"] = config_json

            cols = [c for c in run_data.keys() if c in run_cols]
            vals = [run_data[c] for c in cols]
            cur = conn.execute(
                f"INSERT INTO runs ({', '.join(cols)}) VALUES ({', '.join(['?']*len(cols))})",
                vals,
            )
            run_id = int(cur.lastrowid)

            result_by_path = {x.get("path"): x for x in results if x.get("path")}
            for p in plan:
                path_s = p.get("path")
                if not path_s:
                    continue
                res = result_by_path.get(path_s, {})
                processed = bool(res.get("action"))
                media_path = Path(path_s)
                bak_path = Path(path_s + ".bak")
                a = analyses.get(path_s)

                if processed:
                    before_tracks, _ = _mkvmerge_sub_tracks(mkvmerge_bin, bak_path)
                    size_before = bak_path.stat().st_size if bak_path.exists() else None
                else:
                    before_tracks = []
                    size_before = None

                after_tracks, _ = _mkvmerge_sub_tracks(mkvmerge_bin, media_path)
                size_after = media_path.stat().st_size if media_path.exists() else None
                size_delta = (size_after - size_before) if (size_before is not None and size_after is not None) else None
                size_delta_pct = (float(size_delta) * 100.0 / float(size_before)) if (size_delta is not None and size_before and size_before > 0) else None

                ocr_tasks = p.get("ocr_tasks") or []
                ocr_planned_langs = _uniq_csv([str(x.get("lang") or "und") for x in ocr_tasks]) if ocr_tasks else ""
                ocr_after_tracks = [
                    t for t in after_tracks if t.get("is_text") and ("ocr" in (str(t.get("name") or "")).lower())
                ]
                ocr_after_langs = _uniq_csv([str(t.get("lang") or "und") for t in ocr_after_tracks]) if ocr_after_tracks else ""

                conn.execute(
                    """
                    INSERT INTO files (
                        run_id, path, action, need_transcode, need_subfix, reasons_video_json, subtitle_plan_json,
                        sub_audit_json, ocr_tasks_json, external_text_langs_json, elapsed_sec, output_bytes, hb_exit_code, error,
                        size_before_bytes, size_after_bytes, size_delta_bytes, size_delta_pct,
                        subs_before_text_langs, subs_before_nontext_langs, subs_after_text_langs, subs_after_nontext_langs,
                        ocr_planned_langs, ocr_after_langs, v_codec, v_bitrate_bps, v_width, v_height, duration_sec,
                        container, source_size_bytes
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        path_s,
                        res.get("action"),
                        1 if p.get("need_transcode") else 0,
                        1 if p.get("need_subfix") else 0,
                        _json_dumps(p.get("reasons_video") or []),
                        _json_dumps(p.get("subtitle_plan") or {}),
                        _json_dumps(p.get("sub_audit") or []),
                        _json_dumps(ocr_tasks),
                        _json_dumps(p.get("external_text_langs") or []),
                        float(res.get("elapsed_sec")) if res.get("elapsed_sec") is not None else None,
                        int(res.get("output_bytes")) if res.get("output_bytes") is not None else None,
                        int(res.get("hb_exit_code")) if res.get("hb_exit_code") is not None else None,
                        res.get("error"),
                        size_before,
                        size_after,
                        size_delta,
                        size_delta_pct,
                        _langs_csv(before_tracks, is_text=True),
                        _langs_csv(before_tracks, is_text=False),
                        _langs_csv(after_tracks, is_text=True),
                        _langs_csv(after_tracks, is_text=False),
                        ocr_planned_langs,
                        ocr_after_langs,
                        (a.v_codec if a else None),
                        (a.v_bitrate_bps if a else None),
                        (a.v_width if a else None),
                        (a.v_height if a else None),
                        (a.duration_sec if a else None),
                        (a.container if a else None),
                        (a.size_bytes if a else None),
                    ),
                )

                if sub_cols:
                    file_col = "file_path" if "file_path" in sub_cols else ("path" if "path" in sub_cols else None)
                    default_col = "default_track" if "default_track" in sub_cols else ("is_default" if "is_default" in sub_cols else None)
                    for stage, tracks in (("before", before_tracks), ("after", after_tracks)):
                        for t in tracks:
                            row: Dict[str, Any] = {
                                "run_id": run_id,
                                "stage": stage,
                                "track_id": int(t.get("track_id") or 0),
                                "codec": t.get("codec"),
                                "lang": t.get("lang"),
                                "name": t.get("name"),
                                "forced": int(t.get("forced") or 0),
                                "is_text": int(t.get("is_text") or 0),
                            }
                            if file_col:
                                row[file_col] = path_s
                            if default_col:
                                row[default_col] = int(t.get("default_track") or 0)
                            cols_t = [c for c in row.keys() if c in sub_cols]
                            vals_t = [row[c] for c in cols_t]
                            if cols_t:
                                conn.execute(
                                    f"INSERT INTO subtitle_tracks ({', '.join(cols_t)}) VALUES ({', '.join(['?']*len(cols_t))})",
                                    vals_t,
                                )

            conn.commit()
            return
        except sqlite3.OperationalError as e:
            last_err = e
            msg = str(e).lower()
            if "database is locked" in msg:
                if conn is not None:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    conn.close()
                time.sleep(min(6.0, 0.5 * attempt))
                continue
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    if last_err:
        raise last_err


def get_completed_paths(db_path: Path, run_id: int) -> set[str]:
    """Return paths already successfully processed in a given run (for resume)."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        rows = conn.execute(
            "SELECT path FROM files WHERE run_id=? AND action IN ('transcoded','subfixed')",
            (run_id,),
        ).fetchall()
        conn.close()
        return {r[0] for r in rows if r[0]}
    except Exception:
        return set()


def get_latest_aborted_run(db_path: Path) -> Optional[Dict[str, Any]]:
    """Return the most recent run with status='aborted', or None."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, started, mode, to_process_count, results_count FROM runs WHERE status='aborted' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None
