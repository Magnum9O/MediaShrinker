#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import html
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote, urlencode, urlparse

from mediashrinker_core.run_db import ensure_schema

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    HAVE_APSCHEDULER = True
except ImportError:
    HAVE_APSCHEDULER = False


DEFAULT_DB = Path(os.environ.get("MEDIA_REPORT_DIR", "/reports")) / "mediashrinker_runs.sqlite"


def h(s: Any) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def gib(n: Optional[int]) -> str:
    if n is None:
        return "-"
    return f"{(float(n) / 1024 / 1024 / 1024):.2f}"


def hms(sec: Optional[float]) -> str:
    if sec is None:
        return "-"
    s = int(round(float(sec)))
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"


def pct(v: Optional[float]) -> str:
    if v is None:
        return "-"
    return f"{v:+.1f}%"


def kbps_from_size_duration(size_bytes: Optional[int], duration_sec: Optional[float]) -> Optional[int]:
    try:
        if size_bytes is None or duration_sec is None or float(duration_sec) <= 0:
            return None
        return int(round((float(size_bytes) * 8.0) / float(duration_sec) / 1000.0))
    except Exception:
        return None


def infer_codec_after(action: Optional[str], codec_before: Optional[str], codec_after_db: Optional[str]) -> Optional[str]:
    if codec_after_db:
        return str(codec_after_db)
    if (action or "").strip().lower() == "transcoded":
        return "hevc"
    return codec_before


def compute_size_metrics(
    *,
    size_before_bytes: Optional[int],
    source_size_bytes: Optional[int],
    size_after_bytes: Optional[int],
    output_bytes: Optional[int],
    size_delta_bytes: Optional[int],
    size_delta_pct: Optional[float],
) -> tuple[Optional[int], Optional[int], Optional[int], Optional[float]]:
    before = size_before_bytes if size_before_bytes is not None else source_size_bytes
    after = size_after_bytes if size_after_bytes is not None else output_bytes
    delta_b = size_delta_bytes
    delta_p = size_delta_pct
    if delta_b is None and before is not None and after is not None:
        delta_b = int(after) - int(before)
    if delta_p is None and delta_b is not None and before is not None and int(before) > 0:
        delta_p = float(delta_b) * 100.0 / float(before)
    return before, after, delta_b, delta_p


def display_action(action: Optional[str], need_transcode: Any, need_subfix: Any) -> str:
    a = (action or "").strip()
    if a:
        return a
    if int(need_transcode or 0) == 0 and int(need_subfix or 0) == 0:
        return "already-ok"
    return "planned"


APP_NAME = "MediaShrinker"


BASE_CSS = r"""
  :root{
    --bg0:#0b1020;
    --bg1:#0c1930;
    --paper:rgba(255,255,255,.06);
    --paper2:rgba(255,255,255,.035);
    --ink:#eaf1fb;
    --muted:#a7b6c8;
    --line:rgba(255,255,255,.12);
    --accent:#7cc9ff;
    --accent2:#3ec9a7;
    --ok:#7de2c9;
    --warn:#ffd888;
    --bad:#ff9a9a;
    --shadow:0 1px 0 rgba(0,0,0,.25);
  }
  *{ box-sizing:border-box; }
  body{
    margin:0;
    color:var(--ink);
    font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Apple Color Emoji","Segoe UI Emoji";
    background:
      radial-gradient(1200px 420px at 10% -10%, rgba(91,149,255,.24), transparent 60%),
      radial-gradient(900px 320px at 100% -20%, rgba(62,201,167,.18), transparent 55%),
      linear-gradient(150deg, var(--bg0), var(--bg1));
    min-height:100vh;
  }
  a{ color:var(--accent); text-decoration:none; }
  a:hover{ text-decoration:underline; }
  .wrap{ max-width:1400px; margin:0 auto; padding:18px; }
  .topbar{
    display:flex; gap:12px; align-items:center; justify-content:space-between; flex-wrap:wrap;
    margin-bottom:12px;
  }
  .brand{ display:flex; gap:12px; align-items:baseline; }
  .brand h1{ margin:0; font-size:22px; letter-spacing:.2px; }
  .brand .sub{ color:var(--muted); font-size:12px; }
  .nav{ display:flex; gap:12px; flex-wrap:wrap; align-items:center; }
  .nav a{ color:#cfe6ff; font-size:13px; }
  .nav a.mono{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .card{
    background:var(--paper);
    border:1px solid var(--line);
    border-radius:14px;
    box-shadow:var(--shadow);
    padding:12px;
    margin-bottom:12px;
    backdrop-filter: blur(10px);
  }
  h2{ margin:0 0 8px; font-size:16px; letter-spacing:.2px; }
  .muted{ color:var(--muted); }
  .mono{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .grid{ display:grid; gap:10px; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); }
  table{ width:100%; border-collapse:collapse; font-size:13px; }
  th,td{ border-bottom:1px solid rgba(255,255,255,.10); padding:7px 6px; text-align:left; vertical-align:top; }
  th{ position:sticky; top:0; background:rgba(0,0,0,.22); backdrop-filter: blur(10px); z-index:1; }
  .chip{
    display:inline-block;
    font-size:11px;
    padding:2px 7px;
    border-radius:999px;
    border:1px solid var(--line);
    background:rgba(255,255,255,.04);
    color:var(--muted);
  }
  .chip.ok{ border-color:rgba(62,201,167,.45); color:var(--ok); background:rgba(62,201,167,.14); }
  .chip.warn{ border-color:rgba(244,195,91,.35); color:var(--warn); background:rgba(244,195,91,.12); }
  .chip.bad{ border-color:rgba(255,111,111,.35); color:var(--bad); background:rgba(255,111,111,.12); }
  pre{
    margin:0;
    padding:10px;
    border-radius:10px;
    border:1px solid var(--line);
    background: rgba(0,0,0,.22);
    color: var(--ink);
    overflow:auto;
    font-size:12px;
  }
  input,select,button,textarea{
    font: inherit;
  }
  input,select,textarea{
    width:100%;
    border-radius:10px;
    border:1px solid rgba(255,255,255,.18);
    background: rgba(0,0,0,.22);
    color: var(--ink);
    padding: 9px 10px;
    outline: none;
  }
  input::placeholder{ color: rgba(234,241,251,.55); }
  button{
    border:0;
    border-radius:999px;
    padding:10px 16px;
    font-weight:700;
    cursor:pointer;
    background: linear-gradient(90deg, var(--accent), var(--accent2));
    color: #061018;
  }
  button.secondary{
    background: rgba(255,255,255,.12);
    color: var(--ink);
    border: 1px solid rgba(255,255,255,.18);
  }
  button.danger{
    background: linear-gradient(90deg, #ff7b7b, #ffc36b);
    color: #1d0c0c;
  }
"""


def nav_html() -> str:
    return (
        '<div class="nav">'
        '<a href="/dashboard">dashboard</a>'
        '<a href="/ops">control room</a>'
        '<a href="/live">live</a>'
        '<a href="/schedule">scheduler</a>'
        '<a href="/">runs</a>'
        '<a class="mono" href="/dashboard.json">dashboard.json</a>'
        '<a class="mono" href="/ops.json">ops.json</a>'
        "</div>"
    )


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{h(title)}</title>
  <style>
{BASE_CSS}
  </style>
</head>
<body>
  <div class="wrap">{body}</div>
</body>
</html>
"""


class App:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.app_dir = Path(__file__).resolve().parent
        self.job_lock = threading.RLock()
        self.job_proc: Optional[subprocess.Popen] = None
        self.job_started_at: Optional[float] = None
        self.job_mode: Optional[str] = None
        self.runs_cols: set[str] = set()
        self.files_table: Optional[str] = None
        self.files_cols: set[str] = set()
        self.sub_tracks_table: Optional[str] = None
        self.sub_tracks_file_col: str = "file_path"
        self.sub_tracks_default_col: Optional[str] = "default_track"
        self.refresh_schema()
        self._scheduler: Any = None
        if HAVE_APSCHEDULER:
            self._scheduler = BackgroundScheduler(timezone="UTC")
            self._scheduler.start()
            self._reload_scheduled_jobs()

    def conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def refresh_schema(self) -> None:
        with self.conn() as con:
            tbls = {
                r["name"]
                for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            self.runs_cols = self._table_cols(con, "runs")
            if "files" in tbls:
                self.files_table = "files"
                self.files_cols = self._table_cols(con, "files")
            elif "file_items" in tbls:
                self.files_table = "file_items"
                self.files_cols = self._table_cols(con, "file_items")
            else:
                self.files_table = None
                self.files_cols = set()

            if "subtitle_tracks" in tbls:
                self.sub_tracks_table = "subtitle_tracks"
                sub_cols = self._table_cols(con, "subtitle_tracks")
                self.sub_tracks_file_col = "file_path" if "file_path" in sub_cols else "path"
                if "default_track" in sub_cols:
                    self.sub_tracks_default_col = "default_track"
                elif "is_default" in sub_cols:
                    self.sub_tracks_default_col = "is_default"
                else:
                    self.sub_tracks_default_col = None
            else:
                self.sub_tracks_table = None
                self.sub_tracks_default_col = None

    @staticmethod
    def _table_cols(con: sqlite3.Connection, table: str) -> set[str]:
        try:
            return {r["name"] for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
        except Exception:
            return set()

    @staticmethod
    def _pick(cols: set[str], preferred: List[str], fallback_sql: str = "NULL") -> str:
        for c in preferred:
            if c in cols:
                return c
        return fallback_sql

    @staticmethod
    def _col(cols: set[str], name: str, alias: Optional[str] = None, fallback_sql: str = "NULL") -> str:
        a = alias or name
        return f"{name} AS {a}" if name in cols else f"{fallback_sql} AS {a}"

    def runtime_config(self) -> Dict[str, Any]:
        report_dir = Path(os.environ.get("MEDIA_REPORT_DIR") or str(self.db_path.parent))
        return {
            "movies_dir": os.environ.get("MEDIA_MOVIES_DIR", "/data/movies"),
            "tv_dir": os.environ.get("MEDIA_TV_DIR", "/data/tv"),
            "staging_dir": os.environ.get("MEDIA_STAGING_DIR", "/staging"),
            "report_dir": str(report_dir),
            "library": os.environ.get("MEDIA_LIBRARY", "both"),
            "encoder": os.environ.get("MEDIA_ENCODER", "auto"),
            "jobs": os.environ.get("MEDIA_JOBS", "1"),
            "ocr_engine": os.environ.get("MEDIA_OCR_ENGINE", "pgsrip"),
            "ocr_langs": os.environ.get("MEDIA_OCR_LANGS", "ita,eng"),
            "extract_pgs": os.environ.get("MEDIA_EXTRACT_PGS", "1"),
            "add_external_text_subs": os.environ.get("MEDIA_ADD_EXTERNAL_TEXT_SUBS", "1"),
            "delete_bak": os.environ.get("MEDIA_DELETE_BAK", "0"),
            "bitrate_threshold_mbps": os.environ.get("MEDIA_BITRATE_THRESHOLD_MBPS", "55.0"),
            "bitrate_4k_mbps": os.environ.get("MEDIA_BITRATE_4K_MBPS", "45.0"),
            "no_multipass": os.environ.get("MEDIA_NO_MULTIPASS", "0"),
            "pgsrip_bin": os.environ.get("MEDIA_PGSRIP_BIN", "pgsrip"),
            "tessdata_prefix": os.environ.get("MEDIA_TESSDATA_PREFIX", "/usr/share/tesseract-ocr/5/tessdata"),
            "encoding_profile": os.environ.get("MEDIA_ENCODING_PROFILE", "balanced"),
            "notify_url": os.environ.get("MEDIA_NOTIFY_URL", ""),
        }

    @staticmethod
    def _enabled(value: Any) -> bool:
        return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on")

    def resolve_encoder(self, encoder: str) -> str:
        enc = str(encoder or "auto").strip()
        if enc.lower() != "auto":
            return enc
        try:
            ff = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=10,
            )
            enc_out = ff.stdout or ""
            has_nvenc = "hevc_nvenc" in enc_out
            has_vaapi = "hevc_vaapi" in enc_out
        except Exception:
            has_nvenc = has_vaapi = False
        has_nvidia = Path("/dev/nvidia0").exists() or Path("/proc/driver/nvidia/version").exists()
        has_dri = Path("/dev/dri/renderD128").exists()
        if has_nvenc and has_nvidia:
            return "hevc_nvenc"
        if has_vaapi and has_dri:
            return "hevc_vaapi"
        return "libx265"

    def operation_status(self) -> Dict[str, Any]:
        with self.job_lock:
            proc = self.job_proc
            if proc and proc.poll() is not None:
                self.job_proc = None
                proc = None
            return {
                "managed_running": proc is not None,
                "managed_pid": proc.pid if proc else None,
                "managed_mode": self.job_mode if proc else None,
                "managed_started_at": self.job_started_at if proc else None,
            }

    def build_job_cmd(self, mode: str, form: Dict[str, List[str]], resume_run_id: Optional[int] = None) -> List[str]:
        cfg = self.runtime_config()
        getv = lambda name, fallback: (form.get(name) or [fallback])[-1]
        encoder = self.resolve_encoder(getv("encoder", cfg["encoder"]))
        cmd = [sys.executable, str(self.app_dir / "mediashrinker.py")]
        if mode == "plan":
            cmd += ["--plan"]
        elif mode == "run":
            cmd += ["--run"]
            if self._enabled(getv("delete_bak", cfg["delete_bak"])):
                cmd += ["--delete-bak"]
        elif mode == "cleanup":
            cmd += ["--cleanup-only"]
        else:
            raise ValueError(f"Unsupported mode: {mode}")

        cmd += [
            "--yes",
            "--library", getv("library", cfg["library"]),
            "--movies-root", getv("movies_dir", cfg["movies_dir"]),
            "--series-root", getv("tv_dir", cfg["tv_dir"]),
            "--staging-dir", getv("staging_dir", cfg["staging_dir"]),
            "--report-dir", getv("report_dir", cfg["report_dir"]),
            "--encoder", encoder,
            "--jobs", getv("jobs", cfg["jobs"]),
            "--bitrate-threshold-mbps", getv("bitrate_threshold_mbps", cfg["bitrate_threshold_mbps"]),
            "--bitrate-4k-mbps", getv("bitrate_4k_mbps", cfg["bitrate_4k_mbps"]),
            "--ocr-engine", getv("ocr_engine", cfg["ocr_engine"]),
            "--ocr-target-langs", getv("ocr_langs", cfg["ocr_langs"]),
            "--pgsrip-bin", getv("pgsrip_bin", cfg["pgsrip_bin"]),
            "--tessdata-prefix", getv("tessdata_prefix", cfg["tessdata_prefix"]),
            "--no-save-config",
        ]

        cmd += ["--extract-pgs" if self._enabled(getv("extract_pgs", cfg["extract_pgs"])) else "--no-extract-pgs"]
        cmd += ["--add-external-text-subs" if self._enabled(getv("add_external_text_subs", cfg["add_external_text_subs"])) else "--no-add-external-text-subs"]
        if self._enabled(getv("no_multipass", cfg["no_multipass"])):
            cmd += ["--no-multipass"]
        profile = getv("encoding_profile", cfg.get("encoding_profile", "balanced"))
        cmd += ["--encoding-profile", profile]
        notify_url = getv("notify_url", cfg.get("notify_url", ""))
        if notify_url:
            cmd += ["--notify-url", notify_url]
        if resume_run_id is not None:
            cmd += ["--resume-run-id", str(resume_run_id)]
        return cmd

    def start_job(self, mode: str, form: Dict[str, List[str]], resume_run_id: Optional[int] = None) -> Dict[str, Any]:
        with self.job_lock:
            if self.job_proc and self.job_proc.poll() is None:
                return {"ok": False, "error": "A managed job is already running.", **self.operation_status()}
            cmd = self.build_job_cmd(mode, form, resume_run_id=resume_run_id)
            env = os.environ.copy()
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.app_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            self.job_proc = proc
            self.job_started_at = time.time()
            self.job_mode = mode
            return {"ok": True, "pid": proc.pid, "mode": mode, "cmd": cmd}

    def stop_job(self) -> Dict[str, Any]:
        with self.job_lock:
            proc = self.job_proc
            if not proc or proc.poll() is not None:
                self.job_proc = None
                return {"ok": True, "stopped": False}
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                proc.terminate()
            return {"ok": True, "stopped": True, "pid": proc.pid}

    # ------------------------------------------------------------------
    # Scheduler APScheduler
    # ------------------------------------------------------------------

    def _reload_scheduled_jobs(self) -> None:
        if not self._scheduler or not self.db_path.exists():
            return
        self._scheduler.remove_all_jobs()
        try:
            with self.conn() as con:
                tbls = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if "schedules" not in tbls:
                    return
                rows = con.execute("SELECT * FROM schedules WHERE enabled=1").fetchall()
        except Exception:
            return
        for row in rows:
            try:
                d = dict(row)
                trigger = CronTrigger.from_crontab(d["cron_expr"], timezone="UTC")  # type: ignore[name-defined]
                self._scheduler.add_job(
                    self._run_scheduled_job,
                    trigger=trigger,
                    args=[d["id"]],
                    id=f"sched_{d['id']}",
                    replace_existing=True,
                )
            except Exception:
                pass

    def _run_scheduled_job(self, sched_id: int) -> None:
        try:
            with self.conn() as con:
                row = con.execute("SELECT * FROM schedules WHERE id=?", (sched_id,)).fetchone()
            if not row:
                return
            d = dict(row)
            form: Dict[str, List[str]] = {"library": [d.get("library", "both")]}
            result = self.start_job(d.get("mode", "plan"), form)
            with self.conn() as con:
                now = datetime.now(timezone.utc).isoformat()
                run_id = result.get("run_id")
                con.execute(
                    "UPDATE schedules SET last_run_at=?, last_run_id=? WHERE id=?",
                    (now, run_id, sched_id),
                )
                con.commit()
        except Exception:
            pass

    def add_schedule(self, name: str, cron_expr: str, mode: str, library: str) -> Dict[str, Any]:
        if not HAVE_APSCHEDULER:
            return {"ok": False, "error": "APScheduler not installed"}
        try:
            CronTrigger.from_crontab(cron_expr, timezone="UTC")  # type: ignore[name-defined]
        except Exception as e:
            return {"ok": False, "error": f"Espressione cron non valida: {e}"}
        now = datetime.now(timezone.utc).isoformat()
        with self.conn() as con:
            cur = con.execute(
                "INSERT INTO schedules (name, cron_expr, mode, library, enabled, created_at) VALUES (?,?,?,?,1,?)",
                (name, cron_expr, mode, library, now),
            )
            con.commit()
            sched_id = int(cur.lastrowid)
        self._reload_scheduled_jobs()
        return {"ok": True, "id": sched_id}

    def delete_schedule(self, sched_id: int) -> None:
        with self.conn() as con:
            con.execute("DELETE FROM schedules WHERE id=?", (sched_id,))
            con.commit()
        if self._scheduler:
            try:
                self._scheduler.remove_job(f"sched_{sched_id}")
            except Exception:
                pass

    def toggle_schedule(self, sched_id: int) -> None:
        with self.conn() as con:
            con.execute("UPDATE schedules SET enabled = 1 - enabled WHERE id=?", (sched_id,))
            con.commit()
        self._reload_scheduled_jobs()

    def schedule_page(self, message: str = "") -> str:
        if not HAVE_APSCHEDULER:
            return page(
                "Scheduler",
                "<div class='card'><h2>APScheduler missing</h2>"
                "<p>Install <code>APScheduler&gt;=3.10,&lt;4</code> and rebuild the image.</p></div>",
            )
        try:
            with self.conn() as con:
                rows = con.execute("SELECT * FROM schedules ORDER BY id DESC").fetchall()
            schedules = [dict(r) for r in rows]
        except Exception:
            schedules = []

        rows_html = ""
        for s in schedules:
            status = "enabled" if s.get("enabled") else "paused"
            rows_html += (
                f"<tr>"
                f"<td>{s['id']}</td>"
                f"<td>{h(s.get('name',''))}</td>"
                f"<td class='mono'>{h(s.get('cron_expr',''))}</td>"
                f"<td>{h(s.get('mode',''))}</td>"
                f"<td>{h(s.get('library',''))}</td>"
                f"<td>{h(status)}</td>"
                f"<td>{h(s.get('last_run_at') or '-')}</td>"
                f"<td>"
                f"<form method='post' action='/schedule/toggle' style='display:inline'>"
                f"<input type='hidden' name='id' value='{s['id']}'>"
                f"<button type='submit'>{'Disable' if s.get('enabled') else 'Enable'}</button></form> "
                f"<form method='post' action='/schedule/delete' style='display:inline'>"
                f"<input type='hidden' name='id' value='{s['id']}'>"
                f"<button type='submit' onclick=\"return confirm('Delete schedule?')\">✕</button></form>"
                f"</td>"
                f"</tr>"
            )

        msg_html = f"<div class='card' style='color:var(--ok)'>{h(message)}</div>" if message else ""
        body = f"""
{msg_html}
<div class="topbar">
  <div class="brand">
    <h1>{APP_NAME}</h1>
    <div class="sub">scheduler</div>
  </div>
  {nav_html()}
</div>
<div class="card">
  <h2>Add schedule</h2>
  <form method="post" action="/schedule/add" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px">
    <div><label style="font-size:12px;display:block" class="muted">Name</label><input name="name" placeholder="Nightly run" style="width:100%"></div>
    <div><label style="font-size:12px;display:block" class="muted">Cron expression <small>(UTC)</small></label><input name="cron_expr" placeholder="0 3 * * *" required style="width:100%"></div>
    <div><label style="font-size:12px;display:block" class="muted">Mode</label>
      <select name="mode" style="width:100%;box-sizing:border-box">
        <option value="plan">plan</option>
        <option value="run">run</option>
        <option value="cleanup">cleanup</option>
      </select></div>
    <div><label style="font-size:12px;display:block" class="muted">Library</label>
      <select name="library" style="width:100%;box-sizing:border-box">
        <option value="both">both</option>
        <option value="movies">movies</option>
        <option value="series">series</option>
      </select></div>
    <div style="align-self:end"><button type="submit" style="padding:9px 20px">Add</button></div>
  </form>
  <p class="muted" style="font-size:12px">Examples: <code>0 3 * * *</code> = daily 03:00 &nbsp;|&nbsp;
     <code>0 2 * * 6</code> = Saturday 02:00 &nbsp;|&nbsp; <code>30 1 * * 1-5</code> = Mon-Fri 01:30</p>
</div>
<div class="card">
  <h2>Schedules ({len(schedules)})</h2>
  <table>
    <thead><tr><th>#</th><th>Name</th><th>Cron (UTC)</th><th>Mode</th><th>Library</th><th>Status</th><th>Last run</th><th></th></tr></thead>
    <tbody>{"".join([rows_html]) if schedules else "<tr><td colspan='8'>No schedules</td></tr>"}</tbody>
  </table>
</div>
"""
        return page("Scheduler", body)

    def latest_aborted_run(self) -> Optional[Dict[str, Any]]:
        if not self.db_path.exists():
            return None
        try:
            with self.conn() as con:
                tbls = {r["name"] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                if "runs" not in tbls:
                    return None
                run_cols = self._table_cols(con, "runs")
                if "status" not in run_cols:
                    return None
                row = con.execute(
                    "SELECT id, started, mode, to_process_count, results_count FROM runs WHERE status='aborted' ORDER BY id DESC LIMIT 1"
                ).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def ops_status(self) -> Dict[str, Any]:
        live = self.latest_live_payload()
        cfg = self.runtime_config()
        cfg["effective_encoder"] = self.resolve_encoder(cfg.get("encoder", "auto"))
        return {
            "ok": True,
            "operation": self.operation_status(),
            "config": cfg,
            "live": live,
            "latest_aborted_run": self.latest_aborted_run(),
        }

    def ops_page(self, message: str = "") -> str:
        cfg = self.runtime_config()
        status = self.operation_status()
        live = self.latest_live_payload()
        payload = live.get("payload") or {}
        totals = payload.get("totals") or {}
        running = bool(status.get("managed_running"))
        effective_encoder = self.resolve_encoder(cfg.get("encoder", "auto"))
        aborted_run = self.latest_aborted_run()

        def checked(name: str) -> str:
            return " checked" if self._enabled(cfg.get(name)) else ""

        def selected(current: Any, value: str) -> str:
            return " selected" if str(current or "") == value else ""

        # Unified Control Room UI (global theme + English-only).
        message_html = f"<div class='card' style='color:var(--ok)'>{h(message)}</div>" if message else ""
        stop_html = (
            "<form method='post' action='/ops/stop'><button class='danger' type='submit'>Stop (SIGINT)</button></form>"
            if running
            else ""
        )
        body = f"""
<div class="topbar">
  <div class="brand">
    <h1>{APP_NAME}</h1>
    <div class="sub">control room</div>
  </div>
  {nav_html()}
</div>
{message_html}

<div class="grid">
  <div class="card">
    <h2>Start job</h2>
    <div class="muted" style="font-size:12px;margin-bottom:10px">
      The web server starts the worker process and the worker writes <span class="mono">run-*.json</span> live to
      <span class="mono">{h(cfg['report_dir'])}</span>. The dashboard reads that JSON, so you can operate purely from the UI.
    </div>

    <form method="post" action="/ops/start">
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px">
        <div>
          <label class="muted" style="font-size:12px;display:block;margin-bottom:5px">Library</label>
          <select name="library">
            <option value="both"{selected(cfg['library'], 'both')}>movies + series</option>
            <option value="movies"{selected(cfg['library'], 'movies')}>movies</option>
            <option value="series"{selected(cfg['library'], 'series')}>series</option>
          </select>
        </div>
        <div><label class="muted" style="font-size:12px;display:block;margin-bottom:5px">Parallel jobs</label><input name="jobs" value="{h(cfg['jobs'])}" inputmode="numeric"></div>
        <div><label class="muted" style="font-size:12px;display:block;margin-bottom:5px">Movies path</label><input name="movies_dir" value="{h(cfg['movies_dir'])}"></div>
        <div><label class="muted" style="font-size:12px;display:block;margin-bottom:5px">TV path</label><input name="tv_dir" value="{h(cfg['tv_dir'])}"></div>
        <div><label class="muted" style="font-size:12px;display:block;margin-bottom:5px">Staging path</label><input name="staging_dir" value="{h(cfg['staging_dir'])}"></div>
        <div><label class="muted" style="font-size:12px;display:block;margin-bottom:5px">Reports path</label><input name="report_dir" value="{h(cfg['report_dir'])}"></div>

        <div>
          <label class="muted" style="font-size:12px;display:block;margin-bottom:5px">Encoder</label>
          <select name="encoder">
            <option value="auto"{selected(cfg['encoder'], 'auto')}>auto (effective: {h(effective_encoder)})</option>
            <option value="hevc_nvenc"{selected(cfg['encoder'], 'hevc_nvenc')}>hevc_nvenc (NVIDIA)</option>
            <option value="hevc_vaapi"{selected(cfg['encoder'], 'hevc_vaapi')}>hevc_vaapi (Intel/AMD)</option>
            <option value="libx265"{selected(cfg['encoder'], 'libx265')}>libx265 (CPU)</option>
          </select>
        </div>
        <div>
          <label class="muted" style="font-size:12px;display:block;margin-bottom:5px">Encoding profile</label>
          <select name="encoding_profile">
            <option value="space_saver"{selected(cfg.get('encoding_profile','balanced'), 'space_saver')}>space_saver</option>
            <option value="balanced"{selected(cfg.get('encoding_profile','balanced'), 'balanced')}>balanced</option>
            <option value="quality"{selected(cfg.get('encoding_profile','balanced'), 'quality')}>quality</option>
            <option value="hq"{selected(cfg.get('encoding_profile','balanced'), 'hq')}>hq</option>
          </select>
        </div>

        <div>
          <label class="muted" style="font-size:12px;display:block;margin-bottom:5px">OCR engine</label>
          <select name="ocr_engine">
            <option value="pgsrip"{selected(cfg['ocr_engine'], 'pgsrip')}>pgsrip</option>
            <option value="none"{selected(cfg['ocr_engine'], 'none')}>none</option>
          </select>
        </div>
        <div><label class="muted" style="font-size:12px;display:block;margin-bottom:5px">OCR target langs</label><input name="ocr_langs" value="{h(cfg['ocr_langs'])}" placeholder="ita,eng"></div>
        <div><label class="muted" style="font-size:12px;display:block;margin-bottom:5px">Bitrate threshold (Mbps)</label><input name="bitrate_threshold_mbps" value="{h(cfg['bitrate_threshold_mbps'])}"></div>
        <div><label class="muted" style="font-size:12px;display:block;margin-bottom:5px">4K threshold (Mbps)</label><input name="bitrate_4k_mbps" value="{h(cfg['bitrate_4k_mbps'])}"></div>
        <div><label class="muted" style="font-size:12px;display:block;margin-bottom:5px">pgsrip binary</label><input name="pgsrip_bin" value="{h(cfg['pgsrip_bin'])}"></div>
        <div><label class="muted" style="font-size:12px;display:block;margin-bottom:5px">TESSDATA_PREFIX</label><input name="tessdata_prefix" value="{h(cfg['tessdata_prefix'])}"></div>
        <div><label class="muted" style="font-size:12px;display:block;margin-bottom:5px">ntfy URL (optional)</label><input name="notify_url" value="{h(cfg.get('notify_url',''))}" placeholder="https://ntfy.sh/your-topic"></div>
      </div>

      <div style="display:flex;gap:12px;flex-wrap:wrap;margin:12px 0">
        <input type="hidden" name="extract_pgs" value="0"><label class="muted" style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="extract_pgs" value="1"{checked('extract_pgs')} style="width:auto"> extract PGS / OCR</label>
        <input type="hidden" name="add_external_text_subs" value="0"><label class="muted" style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="add_external_text_subs" value="1"{checked('add_external_text_subs')} style="width:auto"> mux external text subs (when processing)</label>
        <input type="hidden" name="delete_bak" value="0"><label class="muted" style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="delete_bak" value="1"{checked('delete_bak')} style="width:auto"> delete .bak after each successful swap</label>
        <input type="hidden" name="no_multipass" value="0"><label class="muted" style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="no_multipass" value="1"{checked('no_multipass')} style="width:auto"> disable multipass</label>
      </div>

      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button name="mode" value="plan" type="submit">PLAN (dry run)</button>
        <button name="mode" value="run" type="submit">RUN</button>
        <button class="secondary" name="mode" value="cleanup" type="submit">Cleanup .bak only</button>
      </div>
    </form>
  </div>

  <div class="card">
    <h2>Managed job</h2>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:12px">
      <div class="card" style="margin:0;padding:10px;background:var(--paper2)"><div class="muted" style="font-size:12px">State</div><div style="font-size:22px;font-weight:800">{'RUNNING' if running else 'IDLE'}</div></div>
      <div class="card" style="margin:0;padding:10px;background:var(--paper2)"><div class="muted" style="font-size:12px">Mode</div><div style="font-size:22px;font-weight:800">{h(status.get('managed_mode') or '-')}</div></div>
      <div class="card" style="margin:0;padding:10px;background:var(--paper2)"><div class="muted" style="font-size:12px">PID</div><div style="font-size:22px;font-weight:800">{h(status.get('managed_pid') or '-')}</div></div>
      <div class="card" style="margin:0;padding:10px;background:var(--paper2)"><div class="muted" style="font-size:12px">Live status</div><div style="font-size:22px;font-weight:800">{h(payload.get('status') or '-')}</div></div>
      <div class="card" style="margin:0;padding:10px;background:var(--paper2)"><div class="muted" style="font-size:12px">Done / planned</div><div style="font-size:22px;font-weight:800">{h(totals.get('results_done_count', 0))}/{h(totals.get('to_process_count', 0))}</div></div>
      <div class="card" style="margin:0;padding:10px;background:var(--paper2)"><div class="muted" style="font-size:12px">Delta (GiB)</div><div style="font-size:22px;font-weight:800">{gib(totals.get('processed_delta_bytes'))}</div></div>
    </div>
    {stop_html}
    {f"""<form method='post' action='/ops/resume' style='margin-top:10px'>
      <input type='hidden' name='resume_run_id' value='{aborted_run["id"]}'>
      <button type='submit' class='secondary'>Resume run #{aborted_run["id"]} ({h(aborted_run.get("mode") or "run")} {h(aborted_run.get("results_count",0))}/{h(aborted_run.get("to_process_count",0))})</button>
    </form>""" if aborted_run and not running else ""}
    <h2 style="margin-top:16px">Runtime config</h2>
    <pre>{h(json.dumps({**cfg, 'effective_encoder': effective_encoder}, indent=2, ensure_ascii=False))}</pre>
  </div>
</div>
"""
        return page("MediaShrinker Control Room", body)

    def runs_page(self) -> str:
        started_col = self._pick(self.runs_cols, ["started"], "NULL")
        finished_col = self._pick(self.runs_cols, ["finished_at", "generated_at", "started"], "NULL")
        report_col = self._pick(self.runs_cols, ["report_json_path", "report_path"], "NULL")
        wall_col = self._pick(self.runs_cols, ["run_wall_sec", "total_elapsed_sec"], "NULL")
        with self.conn() as con:
            runs = con.execute(
                f"""
                SELECT id,
                       {started_col} AS started,
                       {finished_col} AS finished_at,
                       {self._col(self.runs_cols, 'mode')},
                       {self._col(self.runs_cols, 'plan_count', fallback_sql='0')},
                       {self._col(self.runs_cols, 'to_process_count', fallback_sql='0')},
                       {self._col(self.runs_cols, 'results_count', fallback_sql='0')},
                       {wall_col} AS run_wall_sec,
                       {self._col(self.runs_cols, 'total_elapsed_sec', fallback_sql='0')},
                       {self._col(self.runs_cols, 'total_output_bytes', fallback_sql='0')},
                       {report_col} AS report_json_path
                FROM runs
                ORDER BY id DESC
                """
            ).fetchall()
        rows = []
        for r in runs:
            run_id = int(r["id"])
            mode = r["mode"] or "-"
            rows.append(
                "<tr>"
                f"<td>{run_id}</td>"
                f"<td>{h(r['started'])}</td>"
                f"<td>{h(r['finished_at'])}</td>"
                f"<td>{h(mode)}</td>"
                f"<td>{int(r['plan_count'])}</td>"
                f"<td>{int(r['to_process_count'])}</td>"
                f"<td>{int(r['results_count'])}</td>"
                f"<td>{hms(r['run_wall_sec'])}</td>"
                f"<td>{hms(r['total_elapsed_sec'])}</td>"
                f"<td>{gib(r['total_output_bytes'])}</td>"
                f"<td class='mono'>{h(r['report_json_path'])}</td>"
                f"<td><a href='/run?id={run_id}'>open</a></td>"
                "</tr>"
            )
        body = f"""
<div class="topbar">
  <div class="brand">
    <h1>{APP_NAME}</h1>
    <div class="sub">runs · <span class="mono">{h(self.db_path)}</span></div>
  </div>
  {nav_html()}
</div>
<div class="card">
  <span class="chip">runs: {len(runs)}</span>
</div>
<div class="card">
  <table>
    <thead>
      <tr>
        <th>run_id</th><th>started</th><th>finished</th><th>mode</th><th>plan</th>
        <th>to_process</th><th>results</th><th>wall</th><th>sum elapsed</th>
        <th>output GiB</th><th>report</th><th></th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows) if rows else "<tr><td colspan='12'>No runs in DB</td></tr>"}
    </tbody>
  </table>
</div>
"""
        return page("MediaShrinker Runs", body)

    def run_page(self, run_id: int, params: Dict[str, List[str]]) -> str:
        if not self.files_table:
            return page("Schema Error", "<div class='card'><h2>files/file_items table not found</h2></div>")
        q = (params.get("q") or [""])[0].strip()
        action = (params.get("action") or [""])[0].strip()
        lang = (params.get("lang") or [""])[0].strip()
        has_ocr = (params.get("has_ocr") or [""])[0].strip()
        errors_only = (params.get("errors") or ["0"])[0].strip() in ("1", "true", "yes")
        min_saving = (params.get("min_saving") or [""])[0].strip()
        min_saving_val: Optional[float] = None
        if min_saving:
            try:
                min_saving_val = float(min_saving)
            except Exception:
                min_saving_val = None

        where = ["run_id = ?"]
        bind: List[Any] = [run_id]
        if q:
            where.append("LOWER(path) LIKE ?")
            bind.append(f"%{q.lower()}%")
        if action:
            where.append("action = ?")
            bind.append(action)
        if lang:
            l = f"%{lang.lower()}%"
            where.append(
                "("
                "LOWER(COALESCE(subs_before_text_langs,'')) LIKE ? OR "
                "LOWER(COALESCE(subs_after_text_langs,'')) LIKE ? OR "
                "LOWER(COALESCE(subs_before_nontext_langs,'')) LIKE ? OR "
                "LOWER(COALESCE(subs_after_nontext_langs,'')) LIKE ? OR "
                "LOWER(COALESCE(ocr_planned_langs,'')) LIKE ? OR "
                "LOWER(COALESCE(ocr_after_langs,'')) LIKE ?"
                ")"
            )
            bind.extend([l, l, l, l, l, l])
        if has_ocr == "planned":
            where.append("COALESCE(ocr_planned_langs,'') <> ''")
        elif has_ocr == "after":
            where.append("COALESCE(ocr_after_langs,'') <> ''")
        elif has_ocr == "any":
            where.append("(COALESCE(ocr_planned_langs,'') <> '' OR COALESCE(ocr_after_langs,'') <> '')")
        if errors_only:
            where.append("COALESCE(error,'') <> ''")
        if min_saving_val is not None:
            where.append("size_delta_pct <= ?")
            bind.append(-abs(min_saving_val))

        started_col = self._pick(self.runs_cols, ["started"], "NULL")
        finished_col = self._pick(self.runs_cols, ["finished_at", "generated_at", "started"], "NULL")
        report_json_col = self._pick(self.runs_cols, ["report_json_path", "report_path"], "NULL")
        report_log_col = self._pick(self.runs_cols, ["report_log_path"], "NULL")
        wall_col = self._pick(self.runs_cols, ["run_wall_sec", "total_elapsed_sec"], "NULL")
        cfg_col = self._pick(self.runs_cols, ["config_json"], "'{}'")

        with self.conn() as con:
            run = con.execute(
                f"""
                SELECT id,
                       {started_col} AS started,
                       {finished_col} AS finished_at,
                       {self._col(self.runs_cols, 'mode')},
                       {report_json_col} AS report_json_path,
                       {report_log_col} AS report_log_path,
                       {self._col(self.runs_cols, 'plan_count', fallback_sql='0')},
                       {self._col(self.runs_cols, 'to_process_count', fallback_sql='0')},
                       {self._col(self.runs_cols, 'results_count', fallback_sql='0')},
                       {wall_col} AS run_wall_sec,
                       {self._col(self.runs_cols, 'total_elapsed_sec', fallback_sql='0')},
                       {self._col(self.runs_cols, 'total_output_bytes', fallback_sql='0')},
                       {cfg_col} AS config_json
                FROM runs WHERE id = ?
                """,
                (run_id,),
            ).fetchone()
            files = con.execute(
                f"""
                SELECT
                       {self._col(self.files_cols, 'path')},
                       {self._col(self.files_cols, 'action')},
                       {self._col(self.files_cols, 'need_transcode', fallback_sql='0')},
                       {self._col(self.files_cols, 'need_subfix', fallback_sql='0')},
                       {self._col(self.files_cols, 'elapsed_sec')},
                       {self._col(self.files_cols, 'error')},
                       {self._col(self.files_cols, 'output_bytes')},
                       {self._col(self.files_cols, 'hb_exit_code')},
                       {self._col(self.files_cols, 'size_before_bytes')},
                       {self._col(self.files_cols, 'size_after_bytes')},
                       {self._col(self.files_cols, 'size_delta_bytes')},
                       {self._col(self.files_cols, 'size_delta_pct')},
                       {self._col(self.files_cols, 'subs_before_text_langs')},
                       {self._col(self.files_cols, 'subs_after_text_langs')},
                       {self._col(self.files_cols, 'subs_before_nontext_langs')},
                       {self._col(self.files_cols, 'subs_after_nontext_langs')},
                       {self._col(self.files_cols, 'ocr_planned_langs')},
                       {self._col(self.files_cols, 'ocr_after_langs')},
                       {self._col(self.files_cols, 'duration_sec')},
                       {self._col(self.files_cols, 'container')},
                       {self._col(self.files_cols, 'v_codec')},
                       {self._col(self.files_cols, 'v_codec_after')},
                       {self._col(self.files_cols, 'v_bitrate_bps')},
                       {self._col(self.files_cols, 'v_width')},
                       {self._col(self.files_cols, 'v_height')},
                       {self._col(self.files_cols, 'source_size_bytes')}
                FROM {self.files_table}
                WHERE """
                + " AND ".join(where)
                + """
                ORDER BY path
                """,
                bind,
            ).fetchall()
            files_all = con.execute(
                f"""
                SELECT
                       {self._col(self.files_cols, 'size_before_bytes')},
                       {self._col(self.files_cols, 'size_after_bytes')},
                       {self._col(self.files_cols, 'size_delta_bytes')},
                       {self._col(self.files_cols, 'size_delta_pct')},
                       {self._col(self.files_cols, 'source_size_bytes')},
                       {self._col(self.files_cols, 'output_bytes')}
                FROM {self.files_table}
                WHERE run_id = ?
                """,
                (run_id,),
            ).fetchall()
        if not run:
            return page("Run Not Found", "<div class='card'><h2>Run not found</h2><a href='/'>Back to runs</a></div>")

        def aggregate_sizes(rows_in: List[sqlite3.Row]) -> tuple[int, int, int, Optional[float]]:
            tot_before = 0
            tot_after = 0
            for x in rows_in:
                before_b, after_b, _, _ = compute_size_metrics(
                    size_before_bytes=x["size_before_bytes"],
                    source_size_bytes=x["source_size_bytes"],
                    size_after_bytes=x["size_after_bytes"],
                    output_bytes=x["output_bytes"],
                    size_delta_bytes=x["size_delta_bytes"],
                    size_delta_pct=x["size_delta_pct"],
                )
                if before_b is not None:
                    tot_before += int(before_b)
                if after_b is not None:
                    tot_after += int(after_b)
            tot_delta = tot_after - tot_before
            tot_delta_pct = (float(tot_delta) * 100.0 / float(tot_before)) if tot_before > 0 else None
            return tot_before, tot_after, tot_delta, tot_delta_pct

        all_before_b, all_after_b, all_delta_b, all_delta_p = aggregate_sizes(files_all)
        shown_before_b, shown_after_b, shown_delta_b, shown_delta_p = aggregate_sizes(files)

        chips = (
            f"<span class='chip'>mode: {h(run['mode'])}</span>"
            f"<span class='chip'>plan: {int(run['plan_count'])}</span>"
            f"<span class='chip'>to_process: {int(run['to_process_count'])}</span>"
            f"<span class='chip'>results: {int(run['results_count'])}</span>"
            f"<span class='chip'>wall: {hms(run['run_wall_sec'])}</span>"
            f"<span class='chip'>sum elapsed: {hms(run['total_elapsed_sec'])}</span>"
            f"<span class='chip'>output: {gib(run['total_output_bytes'])} GiB</span>"
            f"<span class='chip'>input total: {gib(all_before_b)} GiB</span>"
            f"<span class='chip'>output total: {gib(all_after_b)} GiB</span>"
            f"<span class='chip'>delta total: {gib(all_delta_b)} GiB ({pct(all_delta_p)})</span>"
            f"<span class='chip'>shown delta: {gib(shown_delta_b)} GiB ({pct(shown_delta_p)})</span>"
        )

        rows = []
        for f in files:
            path = str(f["path"])
            err = f["error"] or ""
            status = display_action(f["action"], f["need_transcode"], f["need_subfix"])
            klass = "bad" if err else ("ok" if status in ("transcoded", "subfixed") else "")
            link = f"/file?run_id={run_id}&path={quote(path)}"
            res = f"{h(f['v_width'])}x{h(f['v_height'])}" if f["v_width"] and f["v_height"] else "-"
            src_kbps = int(f["v_bitrate_bps"]) // 1000 if f["v_bitrate_bps"] else None
            before_b, after_b, delta_b, delta_p = compute_size_metrics(
                size_before_bytes=f["size_before_bytes"],
                source_size_bytes=f["source_size_bytes"],
                size_after_bytes=f["size_after_bytes"],
                output_bytes=f["output_bytes"],
                size_delta_bytes=f["size_delta_bytes"],
                size_delta_pct=f["size_delta_pct"],
            )
            out_kbps = kbps_from_size_duration(after_b, f["duration_sec"])
            vb = f"{src_kbps if src_kbps is not None else '-'} -> {out_kbps if out_kbps is not None else '-'} kbps"
            codec_after = infer_codec_after(f["action"], f["v_codec"], f["v_codec_after"])
            codec_txt = f"{(f['v_codec'] or '-')} -> {(codec_after or '-')}"
            flags = []
            if int(f["need_transcode"] or 0):
                flags.append("T")
            if int(f["need_subfix"] or 0):
                flags.append("S")
            flags_txt = "".join(flags) or "-"
            container = f["container"] or "-"
            rows.append(
                "<tr>"
                f"<td><a href='{link}'>{h(Path(path).name)}</a><div class='small muted mono'>{h(path)}</div></td>"
                f"<td class='{klass}'>{h(status)}</td>"
                f"<td>{h(flags_txt)}</td>"
                f"<td>{hms(f['elapsed_sec'])}</td>"
                f"<td>{hms(f['duration_sec'])}</td>"
                f"<td>{h(container)}</td>"
                f"<td>{h(codec_txt)}</td>"
                f"<td>{h(res)}</td>"
                f"<td>{h(vb)}</td>"
                f"<td>{gib(before_b)}</td>"
                f"<td>{gib(after_b)}</td>"
                f"<td>{gib(delta_b)}</td>"
                f"<td>{pct(delta_p)}</td>"
                f"<td>{gib(f['output_bytes'])}</td>"
                f"<td>{h(f['hb_exit_code'] if f['hb_exit_code'] is not None else '-')}</td>"
                f"<td>{h(f['subs_before_text_langs'] or '-')} -> {h(f['subs_after_text_langs'] or '-')}</td>"
                f"<td>{h(f['subs_before_nontext_langs'] or '-')} -> {h(f['subs_after_nontext_langs'] or '-')}</td>"
                f"<td>{h(f['ocr_planned_langs'] or '-')} -> {h(f['ocr_after_langs'] or '-')}</td>"
                f"<td class='bad'>{h(err)}</td>"
                "</tr>"
            )

        cfg_pre = h(json.dumps(json.loads(run["config_json"] or "{}"), indent=2, ensure_ascii=False))
        filter_query = urlencode(
            {
                "id": run_id,
                "q": q,
                "action": action,
                "lang": lang,
                "has_ocr": has_ocr,
                "errors": "1" if errors_only else "0",
                "min_saving": min_saving,
            }
        )
        body = f"""
<div class="topbar">
  <div class="brand">
    <h1>{APP_NAME}</h1>
    <div class="sub">run #{int(run['id'])}</div>
  </div>
  {nav_html()}
</div>
<div class="card">
  <div class="chips">{chips}</div>
  <p class="small muted mono">report_json: {h(run['report_json_path'])}<br>report_log: {h(run['report_log_path'])}</p>
</div>
<div class="grid">
  <div class="card">
    <h2>Config snapshot</h2>
    <pre>{cfg_pre}</pre>
  </div>
  <div class="card">
    <h2>Legend</h2>
    <p class="small muted">
      <span class="mono">text/non-text</span>: aggregated subtitle languages before/after.<br>
      <span class="mono">ocr</span>: planned languages -> languages found after in the output file (OCR track tag).<br>
      Click a file for per-track details.
    </p>
  </div>
</div>
<div class="card">
  <h2>Filters</h2>
  <form method="get" action="/run" class="small">
    <input type="hidden" name="id" value="{run_id}">
    <div class="chips" style="margin-bottom:8px;">
      <input name="q" value="{h(q)}" placeholder="search (path)" style="min-width:260px;">
      <select name="action">
        <option value="" {"selected" if not action else ""}>action: all</option>
        <option value="transcoded" {"selected" if action=="transcoded" else ""}>transcoded</option>
        <option value="subfixed" {"selected" if action=="subfixed" else ""}>subfixed</option>
        <option value="failed" {"selected" if action=="failed" else ""}>failed</option>
        <option value="skipped" {"selected" if action=="skipped" else ""}>skipped</option>
      </select>
      <input name="lang" value="{h(lang)}" placeholder="language (ita, eng, spa...)">
      <select name="has_ocr">
        <option value="" {"selected" if not has_ocr else ""}>ocr: all</option>
        <option value="planned" {"selected" if has_ocr=="planned" else ""}>planned</option>
        <option value="after" {"selected" if has_ocr=="after" else ""}>after</option>
        <option value="any" {"selected" if has_ocr=="any" else ""}>any</option>
      </select>
      <label class="muted"><input type="checkbox" name="errors" value="1" {"checked" if errors_only else ""} style="width:auto"> errors only</label>
      <input name="min_saving" value="{h(min_saving)}" placeholder="min saving % (e.g. 20)">
      <button type="submit">Apply</button>
      <a href="/run?id={run_id}">Reset</a>
    </div>
  </form>
  <p class="small muted">Showing: <b>{len(files)}</b> files. Shareable link:
  <a class="mono" href="/run?{filter_query}">/run?{h(filter_query)}</a></p>
</div>
<div class="card">
  <table>
    <thead>
      <tr>
        <th>file</th><th>action</th><th>flags</th><th>elapsed</th><th>duration</th><th>container</th><th>codec src->out</th><th>res</th><th>bitrate src->out</th>
        <th>before GiB</th><th>after GiB</th><th>delta GiB</th><th>delta %</th><th>output GiB</th><th>hb rc</th>
        <th>text langs</th><th>non-text langs</th><th>ocr langs</th><th>error</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows) if rows else "<tr><td colspan='19'>No files in this run</td></tr>"}
    </tbody>
  </table>
</div>
"""
        return page(f"MediaShrinker Run {run_id}", body)

    def latest_live_payload(self) -> Dict[str, Any]:
        # 1) Prefer live report JSON directly from reports dir (works before DB final persist).
        reports_dir = self.db_path.parent
        try:
            candidates = sorted(
                reports_dir.glob("run-*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            candidates = []

        for rp in candidates[:20]:
            try:
                p = json.loads(rp.read_text(encoding="utf-8"))
            except Exception:
                continue
            status = str(p.get("status") or "").lower()
            if status == "running":
                return {
                    "ok": True,
                    "run_id": 0,
                    "started": p.get("started"),
                    "report_json_path": str(rp),
                    "payload": p,
                    "source": "report-live",
                }

        # 2) Fallback to latest run linked from DB.
        started_col = self._pick(self.runs_cols, ["started"], "NULL")
        report_col = self._pick(self.runs_cols, ["report_json_path", "report_path"], "NULL")
        with self.conn() as con:
            row = con.execute(
                f"""
                SELECT id, {started_col} AS started, {report_col} AS report_json_path
                FROM runs
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if not row:
            return {"ok": False, "error": "no runs in DB"}

        run_id = int(row["id"])
        report_path_s = row["report_json_path"]
        if not report_path_s:
            return {"ok": False, "error": "latest run has no report_json_path", "run_id": run_id}
        report_path = Path(str(report_path_s))
        if not report_path.exists():
            return {
                "ok": False,
                "error": "report_json file not found",
                "run_id": run_id,
                "report_json_path": str(report_path),
            }
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as e:
            return {
                "ok": False,
                "error": f"invalid report_json: {e}",
                "run_id": run_id,
                "report_json_path": str(report_path),
            }

        return {
            "ok": True,
            "run_id": run_id,
            "started": row["started"],
            "report_json_path": str(report_path),
            "payload": payload,
            "source": "db-latest",
        }

    def live_page(self) -> str:
        live = self.latest_live_payload()
        if not live.get("ok"):
            body = (
                "<div class='topbar'><div class='brand'><h1>MediaShrinker</h1><div class='sub'>live</div></div>"
                + nav_html()
                + "</div>"
                f"<div class='card'><h2>Live not available</h2><pre>{h(json.dumps(live, indent=2, ensure_ascii=False))}</pre></div>"
            )
            return page("MediaShrinker Live", body)

        p = live.get("payload") or {}
        totals = p.get("totals") or {}
        status = p.get("status") or "unknown"
        mode = p.get("mode") or "-"
        run_id = int(live.get("run_id") or 0)
        cfg = p.get("config") or {}
        chips = (
            f"<span class='chip'>run_id: {run_id}</span>"
            f"<span class='chip'>status: {h(status)}</span>"
            f"<span class='chip'>mode: {h(mode)}</span>"
            f"<span class='chip'>wall: {hms(p.get('run_wall_sec'))}</span>"
            f"<span class='chip'>results done: {h(totals.get('results_done_count', 0))}</span>"
            f"<span class='chip'>to_process: {h(totals.get('to_process_count', 0))}</span>"
            f"<span class='chip'>input: {gib(totals.get('processed_input_bytes'))} GiB</span>"
            f"<span class='chip'>output: {gib(totals.get('processed_output_bytes'))} GiB</span>"
            f"<span class='chip'>delta: {gib(totals.get('processed_delta_bytes'))} GiB ({pct(totals.get('processed_delta_pct'))})</span>"
        )
        body = f"""
<div class="topbar">
  <div class="brand">
    <h1>{APP_NAME}</h1>
    <div class="sub">live</div>
  </div>
  {nav_html()}
</div>
<div class="card"><div class="chips">{chips}</div></div>
<div class="grid">
  <div class="card">
    <h2>Config</h2>
    <pre>{h(json.dumps(cfg, indent=2, ensure_ascii=False))}</pre>
  </div>
  <div class="card">
    <h2>Totals</h2>
    <pre>{h(json.dumps(totals, indent=2, ensure_ascii=False))}</pre>
  </div>
</div>
<script>
setTimeout(function() {{ window.location.reload(); }}, 2000);
</script>
"""
        return page("MediaShrinker Live", body)

    def dashboard_data(self) -> Dict[str, Any]:
        live = self.latest_live_payload()
        with self.conn() as con:
            started_col = self._pick(self.runs_cols, ["started"], "NULL")
            finished_col = self._pick(self.runs_cols, ["finished_at", "generated_at", "started"], "NULL")
            recent_runs = con.execute(
                f"""
                SELECT id,
                       {started_col} AS started,
                       {finished_col} AS finished_at,
                       {self._col(self.runs_cols, 'mode')},
                       {self._col(self.runs_cols, 'to_process_count', fallback_sql='0')},
                       {self._col(self.runs_cols, 'results_count', fallback_sql='0')},
                       {self._col(self.runs_cols, 'run_wall_sec', fallback_sql='0')},
                       {self._col(self.runs_cols, 'total_output_bytes', fallback_sql='0')}
                FROM runs
                ORDER BY id DESC
                LIMIT 20
                """
            ).fetchall()

        hist = []
        for r in recent_runs:
            hist.append(
                {
                    "id": int(r["id"]),
                    "started": r["started"],
                    "finished_at": r["finished_at"],
                    "mode": r["mode"] or "-",
                    "to_process_count": int(r["to_process_count"] or 0),
                    "results_count": int(r["results_count"] or 0),
                    "run_wall_sec": float(r["run_wall_sec"] or 0.0),
                    "total_output_bytes": int(r["total_output_bytes"] or 0),
                }
            )

        if not live.get("ok"):
            return {"ok": False, "live": live, "history": hist}

        p = live.get("payload") or {}
        totals = p.get("totals") or {}
        results = p.get("results") or []
        plan = p.get("plan") or []
        jobs_live = p.get("jobs_live") or []

        remaining = max(0, int(totals.get("to_process_count") or 0) - int(totals.get("results_done_count") or 0))
        active_live = [j for j in jobs_live if not bool(j.get("is_idle"))]
        if active_live:
            active_jobs = len(active_live)
        else:
            jobs_cfg = int((p.get("config") or {}).get("jobs") or 1)
            active_jobs = min(max(0, jobs_cfg), remaining) if str(p.get("status") or "").lower() == "running" else 0
        queued_jobs = max(0, remaining - active_jobs)
        done = int(totals.get("results_done_count") or 0)
        failed = sum(1 for x in results if (x.get("action") or "") == "failed" or (x.get("error") or ""))
        pct_done = (float(done) * 100.0 / float(max(1, int(totals.get("to_process_count") or 0)))) if int(totals.get("to_process_count") or 0) > 0 else 0.0

        recent_results = []
        for x in results[-12:]:
            recent_results.append(
                {
                    "path": x.get("path"),
                    "name": Path(str(x.get("path") or "")).name,
                    "action": x.get("action"),
                    "elapsed_sec": x.get("elapsed_sec"),
                    "output_bytes": x.get("output_bytes"),
                    "error": x.get("error"),
                }
            )

        results_paths = {str(rr.get("path")) for rr in results if rr.get("path")}
        pending_all = []
        for pp in plan:
            pth = str(pp.get("path") or "")
            if not pth or pth in results_paths:
                continue

            # Schema-tolerant: prefer the explicit flags, otherwise infer from subtitle_plan.
            if "need_transcode" in pp:
                need_transcode = bool(pp.get("need_transcode"))
            else:
                need_transcode = False

            if "need_subfix" in pp:
                need_subfix = bool(pp.get("need_subfix"))
            else:
                sp = pp.get("subtitle_plan") or {}
                need_subfix = bool(sp.get("need_subfix")) if isinstance(sp, dict) else False

            if not (need_transcode or need_subfix):
                continue

            pending_all.append(
                {
                    "name": Path(pth).name,
                    "path": pth,
                    "need_transcode": need_transcode,
                    "need_subfix": need_subfix,
                    "reasons_video": pp.get("reasons_video") or [],
                }
            )

        if active_live:
            active_cards = [
                {
                    "slot": int(j.get("slot") or 0),
                    "text": str(j.get("text") or ""),
                    "name": "",
                    "estimated": False,
                }
                for j in active_live
            ]
            queue_preview = pending_all[:12]
        else:
            active_cards = [
                {
                    "slot": i + 1,
                    "text": "",
                    "name": x.get("name"),
                    "estimated": True,
                }
                for i, x in enumerate(pending_all[:active_jobs])
            ]
            queue_preview = pending_all[active_jobs : active_jobs + 12]

        return {
            "ok": True,
            "run_id": int(live["run_id"]),
            "started": live.get("started"),
            "report_json_path": live.get("report_json_path"),
            "status": p.get("status"),
            "mode": p.get("mode"),
            "run_wall_sec": p.get("run_wall_sec"),
            "totals": totals,
            "kpi": {
                "converting": active_jobs,
                "queued": queued_jobs,
                "done": done,
                "failed": failed,
                "progress_pct": pct_done,
            },
            "active_jobs_live": active_live,
            "active_cards": active_cards,
            "recent_results": recent_results,
            "pending_cards": queue_preview,
            "history": hist,
        }

    def dashboard_page(self) -> str:
        # Unified dashboard look (same global theme as other pages).
        body = f"""
<div class="topbar">
  <div class="brand">
    <h1>{APP_NAME}</h1>
    <div class="sub">dashboard</div>
  </div>
  {nav_html()}
</div>

<style>
.kpi-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }}
.kpi {{ background:var(--paper2); border:1px solid var(--line); border-radius:14px; padding:12px; }}
.kpi .label {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.7px; }}
.kpi .val {{ font-size:26px; font-weight:800; margin-top:6px; }}
.progress-wrap {{ margin: 12px 0 14px; }}
.progress-head {{ display:flex; justify-content:space-between; color:var(--muted); font-size:12px; margin-bottom:6px; }}
.progress {{ height:14px; border-radius:999px; border:1px solid var(--line); background:rgba(255,255,255,.08); overflow:hidden; }}
.progress > i {{ display:block; height:100%; width:0%; background:linear-gradient(90deg,var(--accent),var(--accent2)); box-shadow:0 0 18px rgba(62,201,167,.24); transition:width .5s ease; }}
.panel {{ background:var(--paper); border:1px solid var(--line); border-radius:14px; padding:12px; }}
.panel h3 {{ margin:0 0 8px; font-size:15px; letter-spacing:.2px; }}
.list {{ max-height: 320px; overflow:auto; }}
.item {{ border-bottom:1px solid rgba(255,255,255,.10); padding:7px 0; }}
.item:last-child {{ border-bottom:0; }}
.item .n {{ font-weight:700; font-size:13px; color:var(--ink); }}
.item .m {{ font-size:12px; color:var(--muted); }}
.two-col {{ display:grid; grid-template-columns:1.2fr 1fr; gap:12px; }}
@media (max-width: 920px) {{ .two-col {{ grid-template-columns:1fr; }} }}
.spark-row {{ display:grid; grid-template-columns:84px 1fr 70px; gap:8px; align-items:center; margin-bottom:6px; }}
.spark-bar {{ height:8px; border-radius:999px; background:rgba(255,255,255,.10); overflow:hidden; }}
.spark-bar i {{ display:block; height:100%; background:linear-gradient(90deg,var(--accent),var(--accent2)); width:0%; }}
</style>

<div class="kpi-grid">
  <div class="kpi"><div class="label">Run</div><div class="val" id="k-run">-</div></div>
  <div class="kpi"><div class="label">Converting</div><div class="val" id="k-conv">0</div></div>
  <div class="kpi"><div class="label">Queued</div><div class="val" id="k-queue">0</div></div>
  <div class="kpi"><div class="label">Done</div><div class="val" id="k-done">0</div></div>
  <div class="kpi"><div class="label">Failed</div><div class="val" id="k-fail">0</div></div>
  <div class="kpi"><div class="label">Input GiB</div><div class="val" id="k-in">0.00</div></div>
  <div class="kpi"><div class="label">Output GiB</div><div class="val" id="k-out">0.00</div></div>
  <div class="kpi"><div class="label">Delta GiB</div><div class="val" id="k-delta">0.00</div></div>
  <div class="kpi"><div class="label">Delta %</div><div class="val" id="k-delta-p">-</div></div>
</div>

<div class="progress-wrap">
  <div class="progress-head">
    <span id="p-status">status: -</span>
    <span id="p-count">0 / 0</span>
  </div>
  <div class="progress"><i id="p-bar"></i></div>
</div>

<div class="two-col">
  <div class="panel">
    <h3>Active (converting now)</h3>
    <div id="active-list" class="list"></div>
  </div>
  <div class="panel">
    <h3>Queue (preview)</h3>
    <div id="queue-list" class="list"></div>
  </div>
</div>

<div class="panel" style="margin-top:12px;">
  <h3>Latest completed</h3>
  <div id="done-list" class="list"></div>
</div>

<div class="panel" style="margin-top:12px;">
  <h3>Run history (last 20)</h3>
  <div id="history"></div>
</div>

<script>
function fmtGiB(n){{ if(n===null||n===undefined) return \"-\"; return (n/1024/1024/1024).toFixed(2); }}
function fmtPct(v){{ if(v===null||v===undefined) return \"-\"; return (v>=0?\"+\":\"\") + v.toFixed(1) + \"%\"; }}
function esc(s){{ return (s===null||s===undefined)?\"\":String(s).replace(/[&<>\\\"']/g, m => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\\\"':'&quot;','\\'':'&#39;'}}[m])); }}
let _dashSeq = 0;
async function loadDash(){{
  const mySeq = ++_dashSeq;
  try{{
    const r = await fetch('/dashboard.json', {{cache:'no-store'}});
    const d = await r.json();
    if(mySeq !== _dashSeq) return;
    if(!d.ok){{
      document.getElementById('k-run').textContent = '-';
      document.getElementById('p-status').textContent = 'status: no-live';
      return;
    }}
    document.getElementById('k-run').textContent = '#' + d.run_id;
    document.getElementById('k-conv').textContent = d.kpi.converting;
    document.getElementById('k-queue').textContent = d.kpi.queued;
    document.getElementById('k-done').textContent = d.kpi.done;
    document.getElementById('k-fail').textContent = d.kpi.failed;
    document.getElementById('k-in').textContent = fmtGiB(d.totals.processed_input_bytes);
    document.getElementById('k-out').textContent = fmtGiB(d.totals.processed_output_bytes);
    document.getElementById('k-delta').textContent = fmtGiB(d.totals.processed_delta_bytes);
    document.getElementById('k-delta-p').textContent = fmtPct(d.totals.processed_delta_pct);
    document.getElementById('p-status').textContent = 'status: ' + (d.status || '-');
    document.getElementById('p-count').textContent = (d.kpi.done||0) + ' / ' + (d.totals.to_process_count||0);
    document.getElementById('p-bar').style.width = Math.max(0, Math.min(100, d.kpi.progress_pct || 0)) + '%';

    const active = (d.active_cards||[]).map(x => {{
      if(x.text) return `<div class=\"item\"><div class=\"n\">J${{x.slot}} · ${{esc(x.text||'')}}</div></div>`;
      const est = x.estimated ? ' <span class=\"chip warn\">estimated</span>' : '';
      return `<div class=\"item\"><div class=\"n\">${{esc(x.name||'')}}</div><div class=\"m\">active job${{est}}</div></div>`;
    }}).join('') || '<div class=\"item m\">No active jobs.</div>';
    document.getElementById('active-list').innerHTML = active;

    const queue = (d.pending_cards||[]).map(x =>
      `<div class=\"item\"><div class=\"n\">${{esc(x.name)}}</div><div class=\"m\">` +
      `${{x.need_transcode?'<span class=\"chip warn\">transcode</span>':''}}` +
      `${{x.need_subfix?'<span class=\"chip ok\">subfix</span>':''}}` +
      `${{esc((x.reasons_video||[]).slice(0,2).join(' | '))}}</div></div>`
    ).join('') || '<div class=\"item m\">Queue is empty.</div>';
    document.getElementById('queue-list').innerHTML = queue;

    const done = (d.recent_results||[]).reverse().map(x =>
      `<div class=\"item\"><div class=\"n\">${{esc(x.name||x.path||'')}}</div><div class=\"m\">` +
      `${{(x.error?'<span class=\"chip bad\">failed</span>':('<span class=\"chip ok\">'+esc(x.action||'done')+'</span>'))}}` +
      `elapsed ${{esc(x.elapsed_sec||0)}}s · output ${{fmtGiB(x.output_bytes)}} GiB</div></div>`
    ).join('') || '<div class=\"item m\">No results yet.</div>';
    document.getElementById('done-list').innerHTML = done;

    const hist = d.history || [];
    const maxOut = Math.max(1, ...hist.map(h => h.total_output_bytes || 0));
    document.getElementById('history').innerHTML = hist.map(h =>
      `<div class=\"spark-row\"><div class=\"m\">#${{h.id}}</div>` +
      `<div class=\"spark-bar\"><i style=\"width:${{Math.round((100*(h.total_output_bytes||0))/maxOut)}}%\"></i></div>` +
      `<div class=\"m\">${{fmtGiB(h.total_output_bytes)}} GiB</div></div>`
    ).join('') || '<div class=\"m\">No history.</div>';
  }}catch(e){{
    document.getElementById('p-status').textContent = 'status: error';
  }}
}}
async function dashLoop(){{ await loadDash(); setTimeout(dashLoop, 2000); }}
dashLoop();
</script>
"""
        return page("MediaShrinker Dashboard", body)

    def file_page(self, run_id: int, path: str) -> str:
        if not self.files_table:
            return page("Schema Error", "<div class='card'><h2>files/file_items table not found</h2></div>")
        with self.conn() as con:
            f = con.execute(
                f"""
                SELECT *
                FROM {self.files_table}
                WHERE run_id = ? AND path = ?
                """,
                (run_id, path),
            ).fetchone()
            if self.sub_tracks_table:
                default_col_sql = (
                    f"{self.sub_tracks_default_col} AS default_track"
                    if self.sub_tracks_default_col
                    else "0 AS default_track"
                )
                tracks = con.execute(
                    f"""
                    SELECT stage, track_id, codec, lang, name, forced, {default_col_sql}, is_text
                    FROM {self.sub_tracks_table}
                    WHERE run_id = ? AND {self.sub_tracks_file_col} = ?
                    ORDER BY stage, track_id
                    """,
                    (run_id, path),
                ).fetchall()
            else:
                tracks = []
        if not f:
            return page("File Not Found", "<div class='card'><h2>File not found</h2><a href='/'>Back</a></div>")

        before_rows, after_rows = [], []
        for t in tracks:
            row = (
                "<tr>"
                f"<td>{int(t['track_id'])}</td>"
                f"<td>{h(t['codec'])}</td>"
                f"<td>{h(t['lang'])}</td>"
                f"<td>{h(t['name'])}</td>"
                f"<td>{'yes' if int(t['is_text']) else 'no'}</td>"
                f"<td>{'yes' if int(t['forced']) else 'no'}</td>"
                f"<td>{'yes' if int(t['default_track']) else 'no'}</td>"
                "</tr>"
            )
            if t["stage"] == "before":
                before_rows.append(row)
            else:
                after_rows.append(row)

        d = dict(f)
        before_b, after_b, delta_b, delta_p = compute_size_metrics(
            size_before_bytes=d.get("size_before_bytes"),
            source_size_bytes=d.get("source_size_bytes"),
            size_after_bytes=d.get("size_after_bytes"),
            output_bytes=d.get("output_bytes"),
            size_delta_bytes=d.get("size_delta_bytes"),
            size_delta_pct=d.get("size_delta_pct"),
        )
        codec_after = infer_codec_after(d.get("action"), d.get("v_codec"), d.get("v_codec_after"))
        codec_txt = f"{d.get('v_codec') or '-'} -> {codec_after or '-'}"
        src_kbps = None
        try:
            if d.get("v_bitrate_bps") is not None:
                src_kbps = int(d.get("v_bitrate_bps")) // 1000
        except Exception:
            src_kbps = None
        out_kbps = kbps_from_size_duration(after_b, d.get("duration_sec"))
        flags = []
        if int(d.get("need_transcode") or 0):
            flags.append("T")
        if int(d.get("need_subfix") or 0):
            flags.append("S")
        flags_txt = "".join(flags) or "-"
        reasons = h(json.dumps(json.loads(d.get("reasons_video_json") or "[]"), indent=2, ensure_ascii=False))
        sub_plan = h(json.dumps(json.loads(d.get("subtitle_plan_json") or "{}"), indent=2, ensure_ascii=False))
        sub_audit = h(json.dumps(json.loads(d.get("sub_audit_json") or "[]"), indent=2, ensure_ascii=False))
        ocr_tasks = h(json.dumps(json.loads(d.get("ocr_tasks_json") or "[]"), indent=2, ensure_ascii=False))
        ext_langs = h(json.dumps(json.loads(d.get("external_text_langs_json") or "[]"), indent=2, ensure_ascii=False))
        technical = {
            "action": d.get("action"),
            "flags": flags_txt,
            "elapsed_sec": d.get("elapsed_sec"),
            "duration_sec": d.get("duration_sec"),
            "container": d.get("container"),
            "v_codec": d.get("v_codec"),
            "v_codec_after": d.get("v_codec_after"),
            "v_width": d.get("v_width"),
            "v_height": d.get("v_height"),
            "v_bitrate_kbps_source": src_kbps,
            "v_bitrate_kbps_estimated_output": out_kbps,
            "size_before_bytes": before_b,
            "size_after_bytes": after_b,
            "size_delta_bytes": delta_b,
            "size_delta_pct": delta_p,
            "output_bytes": d.get("output_bytes"),
            "hb_exit_code": d.get("hb_exit_code"),
            "error": d.get("error"),
        }
        technical_pre = h(json.dumps(technical, indent=2, ensure_ascii=False))

        body = f"""
<div class="top">
  <h1>{h(Path(path).name)}</h1>
  <a href="/run?id={run_id}">Back to run #{run_id}</a>
</div>
<div class="card">
  <div class="chips">
    <span class="chip">action: {h(display_action(d.get('action'), d.get('need_transcode'), d.get('need_subfix')))}</span>
    <span class="chip">flags: {h(flags_txt)}</span>
    <span class="chip">elapsed: {hms(d.get('elapsed_sec'))}</span>
    <span class="chip">duration: {hms(d.get('duration_sec'))}</span>
    <span class="chip">container: {h(d.get('container') or '-')}</span>
    <span class="chip">video: {h(codec_txt)} {h(d.get('v_width') or '-')}x{h(d.get('v_height') or '-')}</span>
    <span class="chip">bitrate src->out: {h(src_kbps if src_kbps is not None else '-')} -> {h(out_kbps if out_kbps is not None else '-')} kbps</span>
    <span class="chip">size: {gib(before_b)} -> {gib(after_b)} GiB</span>
    <span class="chip">delta: {gib(delta_b)} GiB ({pct(delta_p)})</span>
    <span class="chip">hb rc: {h(d.get('hb_exit_code') if d.get('hb_exit_code') is not None else '-')}</span>
    <span class="chip">output: {gib(d.get('output_bytes'))} GiB</span>
  </div>
  <p class="small muted mono">{h(path)}</p>
</div>
<div class="grid">
  <div class="card"><h2>Technical</h2><pre>{technical_pre}</pre></div>
  <div class="card"><h2>Reasons Video</h2><pre>{reasons}</pre></div>
  <div class="card"><h2>Subtitle Plan</h2><pre>{sub_plan}</pre></div>
  <div class="card"><h2>Sub Audit</h2><pre>{sub_audit}</pre></div>
  <div class="card"><h2>OCR Tasks</h2><pre>{ocr_tasks}</pre></div>
  <div class="card"><h2>External Text Langs</h2><pre>{ext_langs}</pre></div>
</div>
<div class="card">
  <h2>Subtitle Tracks BEFORE</h2>
  <table>
    <thead><tr><th>id</th><th>codec</th><th>lang</th><th>name</th><th>text</th><th>forced</th><th>default</th></tr></thead>
    <tbody>{"".join(before_rows) if before_rows else "<tr><td colspan='7'>No tracks</td></tr>"}</tbody>
  </table>
</div>
<div class="card">
  <h2>Subtitle Tracks AFTER</h2>
  <table>
    <thead><tr><th>id</th><th>codec</th><th>lang</th><th>name</th><th>text</th><th>forced</th><th>default</th></tr></thead>
    <tbody>{"".join(after_rows) if after_rows else "<tr><td colspan='7'>No tracks</td></tr>"}</tbody>
  </table>
</div>
"""
        return page(f"MediaShrinker File {Path(path).name}", body)


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            try:
                if parsed.path == "/":
                    content = app.runs_page()
                    self._send(200, content)
                    return
                if parsed.path == "/healthz":
                    self._send_json(
                        200,
                        {
                            "ok": True,
                            "db_path": str(app.db_path),
                            "db_exists": app.db_path.exists(),
                        },
                    )
                    return
                if parsed.path == "/ops":
                    content = app.ops_page()
                    self._send(200, content)
                    return
                if parsed.path == "/ops.json":
                    self._send_json(200, app.ops_status())
                    return
                if parsed.path == "/run":
                    run_id = int((params.get("id") or ["0"])[0])
                    content = app.run_page(run_id, params)
                    self._send(200, content)
                    return
                if parsed.path == "/file":
                    run_id = int((params.get("run_id") or ["0"])[0])
                    path = (params.get("path") or [""])[0]
                    content = app.file_page(run_id, path)
                    self._send(200, content)
                    return
                if parsed.path == "/live":
                    content = app.live_page()
                    self._send(200, content)
                    return
                if parsed.path == "/live.json":
                    self._send_json(200, app.latest_live_payload())
                    return
                if parsed.path == "/dashboard":
                    content = app.dashboard_page()
                    self._send(200, content)
                    return
                if parsed.path == "/dashboard.json":
                    self._send_json(200, app.dashboard_data())
                    return
                if parsed.path == "/schedule":
                    self._send(200, app.schedule_page())
                    return
                self._send(404, page("Not Found", "<div class='card'><h2>404</h2></div>"))
            except Exception as e:
                self._send(500, page("Error", f"<div class='card'><h2>Errore</h2><pre>{h(e)}</pre></div>"))

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                form = parse_qs(raw)
                if parsed.path == "/ops/start":
                    mode = (form.get("mode") or [""])[-1]
                    res = app.start_job(mode, form)
                    if res.get("ok"):
                        self._redirect("/ops")
                    else:
                        self._send(409, app.ops_page(str(res.get("error") or "Job non avviato")))
                    return
                if parsed.path == "/ops/stop":
                    app.stop_job()
                    self._redirect("/ops")
                    return
                if parsed.path == "/ops/resume":
                    resume_run_id = int((form.get("resume_run_id") or ["0"])[-1])
                    res = app.start_job("run", form, resume_run_id=resume_run_id if resume_run_id > 0 else None)
                    if res.get("ok"):
                        self._redirect("/ops")
                    else:
                        self._send(409, app.ops_page(str(res.get("error") or "Impossibile avviare il resume")))
                    return
                if parsed.path == "/schedule/add":
                    name = (form.get("name") or [""])[-1].strip()
                    cron_expr = (form.get("cron_expr") or [""])[-1].strip()
                    mode = (form.get("mode") or ["plan"])[-1].strip()
                    library = (form.get("library") or ["both"])[-1].strip()
                    res = app.add_schedule(name, cron_expr, mode, library)
                    if res.get("ok"):
                        self._redirect("/schedule")
                    else:
                        self._send(400, app.schedule_page(str(res.get("error") or "Errore")))
                    return
                if parsed.path == "/schedule/delete":
                    sched_id = int((form.get("id") or ["0"])[-1])
                    app.delete_schedule(sched_id)
                    self._redirect("/schedule")
                    return
                if parsed.path == "/schedule/toggle":
                    sched_id = int((form.get("id") or ["0"])[-1])
                    app.toggle_schedule(sched_id)
                    self._redirect("/schedule")
                    return
                self._send(404, page("Not Found", "<div class='card'><h2>404</h2></div>"))
            except Exception as e:
                self._send(500, page("Error", f"<div class='card'><h2>Errore</h2><pre>{h(e)}</pre></div>"))

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _redirect(self, location: str) -> None:
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send(self, code: int, body: str) -> None:
            data = body.encode("utf-8", errors="replace")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, code: int, obj: Any) -> None:
            data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8", errors="replace")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Web UI for MediaShrinker Run DB")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite DB path (default: {DEFAULT_DB})")
    ap.add_argument("--host", default="127.0.0.1", help="Bind host")
    ap.add_argument("--port", type=int, default=8787, help="Bind port")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    db_path = args.db.expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        ensure_schema(conn)
    app = App(db_path)
    handler = make_handler(app)
    srv = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"MediaShrinker web UI on http://{args.host}:{args.port} (db={db_path})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
