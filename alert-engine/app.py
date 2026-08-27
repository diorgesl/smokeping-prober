#!/usr/bin/env python3
from __future__ import annotations

import html
import io
import json
import logging
import math
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont


LOG = logging.getLogger("smokeping-alert-engine")
STOP = threading.Event()


def env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@dataclass(frozen=True)
class Settings:
    prometheus_url: str = os.getenv("PROMETHEUS_URL", "http://prometheus-smokeping:9090")
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    db_path: str = os.getenv("DATABASE_PATH", "/data/alerts.db")
    evaluation_interval: int = env_int("EVALUATION_INTERVAL_SECONDS", 60)
    query_window: str = os.getenv("QUERY_WINDOW", "5m")
    min_samples: int = env_int("MIN_SAMPLES", 240)
    confirmations: int = env_int("ALERT_CONFIRMATIONS", 3)
    recovery_confirmations: int = env_int("RECOVERY_CONFIRMATIONS", 3)
    loss_threshold: float = env_float("LOSS_THRESHOLD_PERCENT", 5.0)
    critical_loss: float = env_float("CRITICAL_LOSS_PERCENT", 20.0)
    down_loss: float = env_float("DOWN_LOSS_PERCENT", 99.9)
    recovery_loss: float = env_float("RECOVERY_LOSS_PERCENT", 2.0)
    loss_step: float = env_float("LOSS_AGGRAVATION_STEP_PERCENT", 5.0)
    latency_increase_ms: float = env_float("LATENCY_INCREASE_MS", 10.0)
    latency_increase_percent: float = env_float("LATENCY_INCREASE_PERCENT", 25.0)
    jitter_increase_ms: float = env_float("JITTER_INCREASE_MS", 5.0)
    jitter_increase_percent: float = env_float("JITTER_INCREASE_PERCENT", 50.0)
    baseline_alpha: float = env_float("BASELINE_ALPHA", 0.05)
    baseline_min_samples: int = env_int("BASELINE_MIN_EVALUATIONS", 5)
    group_threshold: int = env_int("GROUP_ALERT_THRESHOLD", 3)
    reminders_enabled: bool = env_bool("REMINDERS_ENABLED", True)
    mtr_enabled: bool = env_bool("MTR_ENABLED", True)
    mtr_cycles: int = env_int("MTR_CYCLES", 10)
    mtr_timeout: int = env_int("MTR_TIMEOUT_SECONDS", 90)
    mtr_cooldown: int = env_int("MTR_COOLDOWN_SECONDS", 900)
    grafana_dashboard_url: str = os.getenv("GRAFANA_DASHBOARD_URL", "").rstrip("/")
    timezone: str = os.getenv("TZ", "America/Campo_Grande")
    dry_run: bool = env_bool("DRY_RUN", False)

    def validate(self) -> None:
        if not self.dry_run and (not self.telegram_token or not self.telegram_chat_id):
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
        if not 0 < self.baseline_alpha <= 1:
            raise ValueError("BASELINE_ALPHA must be between 0 and 1")
        if not 0 <= self.loss_threshold < self.critical_loss < self.down_loss <= 100:
            raise ValueError("Loss thresholds must satisfy warning < critical < down <= 100")


@dataclass
class Measurement:
    host: str
    title: str
    category: str
    samples: float
    loss: float
    latency: float | None
    jitter: float | None = None

    @property
    def key(self) -> str:
        return self.host


@dataclass
class Decision:
    measurement: Measurement
    kind: str
    baseline: float | None
    incident_started: float | None = None
    reason: str = ""
    jitter_baseline: float | None = None
    severity: str = "warning"


SCHEMA = """
CREATE TABLE IF NOT EXISTS target_state (
  host TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  category TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'normal',
  severity TEXT NOT NULL DEFAULT 'normal',
  baseline_latency REAL,
  baseline_jitter REAL,
  baseline_samples INTEGER NOT NULL DEFAULT 0,
  anomaly_confirmations INTEGER NOT NULL DEFAULT 0,
  recovery_confirmations INTEGER NOT NULL DEFAULT 0,
  incident_started REAL,
  last_notified REAL,
  next_reminder REAL,
  worst_loss REAL NOT NULL DEFAULT 0,
  worst_latency REAL,
  worst_jitter REAL,
  notified_loss_bucket INTEGER NOT NULL DEFAULT 0,
  notified_latency_loss INTEGER NOT NULL DEFAULT 0,
  notified_jitter INTEGER NOT NULL DEFAULT 0,
  last_mtr REAL,
  last_loss REAL NOT NULL DEFAULT 0,
  last_latency REAL,
  last_jitter REAL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at REAL NOT NULL,
  host TEXT,
  event TEXT NOT NULL,
  payload TEXT NOT NULL,
  delivered INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
"""


class Store:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(target_state)")}
        additions = {
            "severity": "TEXT NOT NULL DEFAULT 'normal'",
            "baseline_jitter": "REAL",
            "worst_jitter": "REAL",
            "notified_jitter": "INTEGER NOT NULL DEFAULT 0",
            "last_jitter": "REAL",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.db.execute(f"ALTER TABLE target_state ADD COLUMN {name} {definition}")

    def begin(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.db.execute("COMMIT")

    def rollback(self) -> None:
        self.db.execute("ROLLBACK")

    def get(self, m: Measurement) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM target_state WHERE host=?", (m.host,)).fetchone()
        if row is None:
            now = time.time()
            self.db.execute(
                "INSERT INTO target_state(host,title,category,updated_at) VALUES(?,?,?,?)",
                (m.host, m.title, m.category, now),
            )
            row = self.db.execute("SELECT * FROM target_state WHERE host=?", (m.host,)).fetchone()
        return row

    def update(self, host: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        sql = ",".join(f"{key}=?" for key in fields)
        self.db.execute(f"UPDATE target_state SET {sql} WHERE host=?", (*fields.values(), host))

    def log_notification(self, host: str | None, event: str, payload: str, delivered: bool, error: str = "") -> None:
        self.db.execute(
            "INSERT INTO notification_log(created_at,host,event,payload,delivered,error) VALUES(?,?,?,?,?,?)",
            (time.time(), host, event, payload, int(delivered), error),
        )


class Prometheus:
    def __init__(self, settings: Settings):
        self.base = settings.prometheus_url.rstrip("/")
        self.window = settings.query_window
        self.http = requests.Session()

    def query(self, expression: str) -> list[dict[str, Any]]:
        response = self.http.get(
            f"{self.base}/api/v1/query", params={"query": expression}, timeout=30
        )
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus query failed: {body}")
        return body["data"]["result"]

    @staticmethod
    def values(rows: list[dict[str, Any]]) -> dict[str, tuple[dict[str, str], float]]:
        output = {}
        for row in rows:
            labels = row["metric"]
            try:
                value = float(row["value"][1])
            except (ValueError, TypeError):
                continue
            output[labels.get("host", "")] = (labels, value)
        return output

    def measurements(self) -> list[Measurement]:
        group = "title,host,category"
        selector = 'alerts_enabled="true"'
        sent = self.values(self.query(
            f"sum by ({group}) (increase(smokeping_requests_total{{{selector}}}[{self.window}]))"
        ))
        received = self.values(self.query(
            f"sum by ({group}) (increase(smokeping_response_duration_seconds_count{{{selector}}}[{self.window}]))"
        ))
        duration = self.values(self.query(
            f"sum by ({group}) (increase(smokeping_response_duration_seconds_sum{{{selector}}}[{self.window}]))"
        ))
        bucket_group = f"le,{group}"
        buckets = (
            f"sum by ({bucket_group}) "
            f"(increase(smokeping_response_duration_seconds_bucket{{{selector}}}[{self.window}]))"
        )
        p50 = self.values(self.query(f"histogram_quantile(0.50, {buckets})"))
        p95 = self.values(self.query(f"histogram_quantile(0.95, {buckets})"))
        measurements = []
        for host, (labels, samples) in sent.items():
            if not host or samples <= 0:
                continue
            replies = received.get(host, ({}, 0.0))[1]
            total_seconds = duration.get(host, ({}, 0.0))[1]
            loss = max(0.0, min(100.0, 100.0 * (1.0 - replies / samples)))
            latency = (1000.0 * total_seconds / replies) if replies > 0 else None
            q50 = p50.get(host, ({}, float("nan")))[1]
            q95 = p95.get(host, ({}, float("nan")))[1]
            jitter = 1000.0 * max(0.0, q95 - q50) if math.isfinite(q50) and math.isfinite(q95) else None
            measurements.append(Measurement(
                host=host,
                title=labels.get("title", host),
                category=labels.get("category", "sem-categoria"),
                samples=samples,
                loss=loss,
                latency=latency,
                jitter=jitter,
            ))
        return measurements


class Telegram:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.base = f"https://api.telegram.org/bot{settings.telegram_token}"
        self.http = requests.Session()

    def send(self, text: str, host: str | None = None, event: str = "message", record: bool = True) -> None:
        if self.settings.dry_run:
            LOG.warning("DRY RUN Telegram:\n%s", text)
            if record:
                self.store.log_notification(host, event, text, True)
            return
        try:
            response = self.http.post(
                f"{self.base}/sendMessage",
                json={
                    "chat_id": self.settings.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            response.raise_for_status()
            if record:
                self.store.log_notification(host, event, text, True)
        except Exception as exc:
            if record:
                self.store.log_notification(host, event, text, False, str(exc))
            raise

    def photo(self, image: bytes, caption: str, host: str) -> None:
        if self.settings.dry_run:
            LOG.warning("DRY RUN MTR photo for %s", host)
            return
        response = self.http.post(
            f"{self.base}/sendPhoto",
            data={"chat_id": self.settings.telegram_chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"photo": ("mtr.png", image, "image/png")},
            timeout=60,
        )
        response.raise_for_status()


def fmt_latency(value: float | None) -> str:
    return "sem resposta" if value is None else f"{value:.1f} ms"


def duration_text(seconds: float) -> str:
    minutes = max(1, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} minuto{'s' if minutes != 1 else ''}"
    hours, remainder = divmod(minutes, 60)
    return f"{hours}h{remainder:02d}"


def latency_detail(baseline: float | None, current: float | None) -> str:
    if current is None:
        return "📶 Latência: sem resposta"
    if baseline and baseline > 0:
        percent = 100.0 * (current - baseline) / baseline
        arrow = "⬆️" if percent >= 0 else "⬇️"
        return f"📶 Latência: {baseline:.1f} → {current:.1f} ms  {arrow} {percent:+.0f}%"
    return f"📶 Latência atual: {current:.1f} ms"


def jitter_detail(baseline: float | None, current: float | None) -> str:
    if current is None:
        return "〰️ Jitter: indisponível"
    if baseline is not None:
        return f"〰️ Jitter: {baseline:.1f} → {current:.1f} ms"
    return f"〰️ Jitter atual: {current:.1f} ms"


def target_heading(m: Measurement) -> str:
    return f"🌐 <b>{html.escape(m.title)}</b> — <code>{html.escape(m.host)}</code>"


class MTRWorker:
    def __init__(self, settings: Settings, store: Store, telegram: Telegram):
        self.settings = settings
        self.store = store
        self.telegram = telegram
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mtr")
        self.running: set[str] = set()
        self.lock = threading.Lock()

    def submit(self, decision: Decision) -> None:
        if not self.settings.mtr_enabled:
            return
        row = self.store.get(decision.measurement)
        last = row["last_mtr"] or 0
        if time.time() - last < self.settings.mtr_cooldown:
            return
        with self.lock:
            if decision.measurement.host in self.running:
                return
            self.running.add(decision.measurement.host)
        self.store.update(decision.measurement.host, last_mtr=time.time())
        self.pool.submit(self._run, decision)

    def _run(self, decision: Decision) -> None:
        host = decision.measurement.host
        completed = None
        try:
            family = "-6" if ":" in host else "-4"
            command = [
                # ICMP Echo is mtr's default. Do not pass --icmp: some
                # mtr-tiny builds do not implement that long option.
                "mtr", family, "-n", "-r", "-w",
                "-c", str(self.settings.mtr_cycles), host,
            ]
            completed = subprocess.run(command, capture_output=True, text=True, timeout=self.settings.mtr_timeout)
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip() or "mtr failed")
            report = self._parse_text_report(completed.stdout)
            if not report["report"]["hubs"]:
                raise RuntimeError("mtr returned no parseable hops")
            image = self._render(report, decision.measurement)
            caption = self._caption(decision, self.settings.mtr_cycles)
            self.telegram.photo(image, caption, host)
        except Exception as exc:
            LOG.exception("MTR failed for %s", host)
            fallback = completed.stdout if completed and completed.stdout else str(exc)
            fallback = fallback[-3500:]
            try:
                self.telegram.send(
                    "🧭 <b>DIAGNÓSTICO MTR — fallback texto</b>\n\n"
                    f"🌐 <b>{html.escape(decision.measurement.title)}</b> — <code>{html.escape(host)}</code>\n"
                    f"<pre>{html.escape(fallback)}</pre>",
                    host,
                    "mtr_text",
                    False,
                )
            except Exception:
                LOG.exception("Could not send MTR text fallback for %s", host)
        finally:
            with self.lock:
                self.running.discard(host)

    @staticmethod
    def _caption(decision: Decision, cycles: int = 10) -> str:
        m = decision.measurement
        reason = {
            "initial": "ALERTA INICIAL",
            "unavailable": "DESTINO INDISPONÍVEL",
            "aggravated": "AGRAVAMENTO",
        }.get(decision.kind, decision.kind.upper())
        if decision.kind == "aggravated" and decision.reason:
            reason += f" — {decision.reason}"

        if m.latency is None:
            latency = "📶 Latência: sem resposta"
        elif decision.baseline and decision.baseline > 0:
            variation = 100 * (m.latency - decision.baseline) / decision.baseline
            arrow = "⬆️" if variation >= 0 else "⬇️"
            latency = (
                f"📶 Latência: {decision.baseline:.1f} → {m.latency:.1f} ms "
                f"{arrow} {variation:+.0f}%"
            )
        else:
            latency = f"📶 Latência atual: {m.latency:.1f} ms"

        started = decision.incident_started or time.time()
        return (
            "🖼️ <b>DIAGNÓSTICO MTR</b>\n\n"
            f"🌐 <b>{html.escape(m.title)}</b> — <code>{html.escape(m.host)}</code>\n"
            f"Motivo: <b>{html.escape(reason)}</b>\n\n"
            f"{latency}\n"
            f"{jitter_detail(decision.jitter_baseline, m.jitter)}\n"
            f"📉 Perda detectada: <b>{m.loss:.1f}%</b>\n"
            f"🕐 Incidente iniciado às {datetime.fromtimestamp(started).strftime('%H:%M')}\n\n"
            f"Ciclos: {cycles} · DNS desativado"
        )

    @staticmethod
    def _parse_text_report(output: str) -> dict[str, Any]:
        """Parse the portable text report emitted by mtr-tiny.

        Example: 1.|-- 192.0.2.1  0.0%  10  0.1  0.2  0.1  0.5  0.1
        Columns after Snt are Last, Avg, Best, Wrst and StDev.
        """
        pattern = re.compile(
            r"^\s*(?P<hop>\d+)\.\|--\s+"
            r"(?P<host>\S+)\s+"
            r"(?P<loss>[\d.]+)%?\s+"
            r"(?P<sent>\d+)\s+"
            r"(?P<last>[\d.]+)\s+"
            r"(?P<avg>[\d.]+)\s+"
            r"(?P<best>[\d.]+)\s+"
            r"(?P<worst>[\d.]+)\s+"
            r"(?P<stddev>[\d.]+)\s*$"
        )
        hubs = []
        for line in output.splitlines():
            match = pattern.match(line)
            if not match:
                continue
            values = match.groupdict()
            hubs.append({
                "count": int(values["hop"]),
                "host": values["host"],
                "Loss%": float(values["loss"]),
                "Snt": int(values["sent"]),
                "Last": float(values["last"]),
                "Avg": float(values["avg"]),
                "Best": float(values["best"]),
                "Wrst": float(values["worst"]),
                "StDev": float(values["stddev"]),
            })
        return {"report": {"hubs": hubs}}

    @staticmethod
    def _render(report: dict[str, Any], m: Measurement) -> bytes:
        def metric(value: Any) -> str:
            try:
                return f"{float(value):.1f} ms"
            except (TypeError, ValueError):
                return "-"

        hubs = report.get("report", {}).get("hubs", [])
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        font = ImageFont.truetype(font_path, 18)
        small = ImageFont.truetype(font_path, 15)
        bold = ImageFont.truetype(bold_path, 20)
        width = 900
        row_h = 34
        height = 105 + row_h * max(1, len(hubs))
        image = Image.new("RGB", (width, height), "#101820")
        draw = ImageDraw.Draw(image)
        draw.text((20, 15), f"MTR - {m.title} - {m.host}", font=bold, fill="#ffffff")
        columns = [(20, "Hop"), (75, "Host"), (470, "Loss"), (555, "Sent"), (625, "Avg"), (710, "Best"), (795, "Worst")]
        for x, name in columns:
            draw.text((x, 62), name, font=font, fill="#5cc8ff")
        for index, hub in enumerate(hubs):
            y = 98 + index * row_h
            if index % 2:
                draw.rectangle((10, y - 4, width - 10, y + row_h - 5), fill="#182733")
            values = [
                str(hub.get("count", index + 1)), str(hub.get("host", "???")),
                f"{hub.get('Loss%', 0)}%", str(hub.get("Snt", "-")),
                metric(hub.get("Avg")), metric(hub.get("Best")), metric(hub.get("Wrst")),
            ]
            for (x, _), value in zip(columns, values):
                draw.text((x, y), value, font=small, fill="#e8eef2")
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()


class Engine:
    def __init__(self, settings: Settings):
        self.s = settings
        self.store = Store(settings.db_path)
        self.prometheus = Prometheus(settings)
        self.telegram = Telegram(settings, self.store)
        self.mtr = MTRWorker(settings, self.store, self.telegram)

    def anomaly(
        self,
        m: Measurement,
        baseline: float | None,
        jitter_baseline: float | None,
        baseline_samples: int,
    ) -> tuple[bool, bool, bool, bool]:
        loss_bad = m.loss >= self.s.loss_threshold
        latency_bad = False
        jitter_bad = False
        if baseline and m.latency is not None and baseline_samples >= self.s.baseline_min_samples:
            delta = m.latency - baseline
            percent = 100.0 * delta / baseline
            latency_bad = delta >= self.s.latency_increase_ms and percent >= self.s.latency_increase_percent
        if jitter_baseline is not None and m.jitter is not None and baseline_samples >= self.s.baseline_min_samples:
            delta = m.jitter - jitter_baseline
            percent = 100.0 * delta / jitter_baseline if jitter_baseline > 0 else (math.inf if delta > 0 else 0)
            jitter_bad = delta >= self.s.jitter_increase_ms and percent >= self.s.jitter_increase_percent
        return loss_bad or latency_bad or jitter_bad, loss_bad, latency_bad, jitter_bad

    def severity(self, m: Measurement, latency_bad: bool, jitter_bad: bool) -> str:
        if m.loss >= self.s.down_loss:
            return "down"
        if m.loss >= self.s.critical_loss or (latency_bad and jitter_bad):
            return "critical"
        return "warning"

    def process(self, m: Measurement, now: float) -> Decision | None:
        row = self.store.get(m)
        baseline = row["baseline_latency"]
        jitter_baseline = row["baseline_jitter"]
        baseline_samples = row["baseline_samples"]
        bad, loss_bad, latency_bad, jitter_bad = self.anomaly(
            m, baseline, jitter_baseline, baseline_samples
        )
        severity = self.severity(m, latency_bad, jitter_bad)
        state = row["state"]
        common = {
            "title": m.title,
            "category": m.category,
            "last_loss": m.loss,
            "last_latency": m.latency,
            "last_jitter": m.jitter,
        }

        if state == "normal":
            if bad and m.samples >= self.s.min_samples:
                count = row["anomaly_confirmations"] + 1
                if count >= self.s.confirmations:
                    bucket = int(m.loss // self.s.loss_step) * int(self.s.loss_step)
                    self.store.update(m.host, **common, state="incident", anomaly_confirmations=0,
                                      recovery_confirmations=0, incident_started=now, last_notified=now,
                                      next_reminder=now + 600, worst_loss=m.loss, worst_latency=m.latency,
                                      worst_jitter=m.jitter, severity=severity,
                                      notified_loss_bucket=bucket, notified_latency_loss=int(latency_bad),
                                      notified_jitter=int(jitter_bad))
                    reason = "unavailable" if severity == "down" else "initial"
                    return Decision(m, reason, baseline, now, jitter_baseline=jitter_baseline, severity=severity)
                self.store.update(m.host, **common, state="pending", anomaly_confirmations=count)
                return None
            self._update_normal(m, row, common)
            return None

        if state == "pending":
            if not bad or m.samples < self.s.min_samples:
                self.store.update(m.host, **common, state="normal", anomaly_confirmations=0)
                self._baseline(m, row)
                return None
            count = row["anomaly_confirmations"] + 1
            if count >= self.s.confirmations:
                bucket = int(m.loss // self.s.loss_step) * int(self.s.loss_step)
                self.store.update(m.host, **common, state="incident", anomaly_confirmations=0,
                                  incident_started=now, last_notified=now, next_reminder=now + 600,
                                  worst_loss=m.loss, worst_latency=m.latency,
                                  worst_jitter=m.jitter, severity=severity,
                                  notified_loss_bucket=bucket, notified_latency_loss=int(latency_bad),
                                  notified_jitter=int(jitter_bad))
                reason = "unavailable" if severity == "down" else "initial"
                return Decision(m, reason, baseline, now, jitter_baseline=jitter_baseline, severity=severity)
            self.store.update(m.host, **common, anomaly_confirmations=count)
            return None

        if state in {"incident", "recovering"}:
            started = row["incident_started"] or now
            if bad:
                self.store.update(m.host, **common, state="incident", recovery_confirmations=0, severity=severity)
                reason = self._aggravation(row, m, loss_bad, latency_bad, jitter_bad, severity)
                if reason:
                    bucket = max(row["notified_loss_bucket"], int(m.loss // self.s.loss_step) * int(self.s.loss_step))
                    worst_latency = max(filter(lambda x: x is not None, [row["worst_latency"], m.latency]), default=None)
                    worst_jitter = max(filter(lambda x: x is not None, [row["worst_jitter"], m.jitter]), default=None)
                    self.store.update(m.host, last_notified=now, worst_loss=max(row["worst_loss"], m.loss),
                                      worst_latency=worst_latency, worst_jitter=worst_jitter,
                                      notified_loss_bucket=bucket, severity=severity,
                                      notified_latency_loss=int(row["notified_latency_loss"] or latency_bad),
                                      notified_jitter=int(row["notified_jitter"] or jitter_bad))
                    return Decision(m, "aggravated", baseline, started, reason,
                                    jitter_baseline=jitter_baseline, severity=severity)
                if self.s.reminders_enabled and row["next_reminder"] and now >= row["next_reminder"]:
                    self.store.update(m.host, last_notified=now, next_reminder=self._next_reminder(started, now))
                    return Decision(m, "persistent", baseline, started,
                                    jitter_baseline=jitter_baseline, severity=severity)
                return None

            count = row["recovery_confirmations"] + 1
            if count >= self.s.recovery_confirmations:
                self.store.update(m.host, **common, state="normal", anomaly_confirmations=0,
                                  recovery_confirmations=0, incident_started=None, last_notified=now,
                                  next_reminder=None, worst_loss=0, worst_latency=None,
                                  worst_jitter=None, severity="normal",
                                  notified_loss_bucket=0, notified_latency_loss=0, notified_jitter=0)
                self._baseline(m, row)
                return Decision(m, "recovered", baseline, started,
                                jitter_baseline=jitter_baseline, severity=row["severity"])
            self.store.update(m.host, **common, state="recovering", recovery_confirmations=count)
        return None

    def _baseline(self, m: Measurement, row: sqlite3.Row) -> None:
        if m.latency is None or m.loss > self.s.recovery_loss:
            return
        old_latency = row["baseline_latency"]
        latency = (
            m.latency if old_latency is None
            else (1 - self.s.baseline_alpha) * old_latency + self.s.baseline_alpha * m.latency
        )
        fields: dict[str, Any] = {
            "baseline_latency": latency,
            "baseline_samples": row["baseline_samples"] + 1,
        }
        if m.jitter is not None:
            old_jitter = row["baseline_jitter"]
            fields["baseline_jitter"] = (
                m.jitter if old_jitter is None
                else (1 - self.s.baseline_alpha) * old_jitter + self.s.baseline_alpha * m.jitter
            )
        self.store.update(m.host, **fields)

    def _update_normal(self, m: Measurement, row: sqlite3.Row, common: dict[str, Any]) -> None:
        self.store.update(m.host, **common, state="normal", anomaly_confirmations=0, recovery_confirmations=0)
        self._baseline(m, row)

    def _aggravation(
        self,
        row: sqlite3.Row,
        m: Measurement,
        loss_bad: bool,
        latency_bad: bool,
        jitter_bad: bool,
        severity: str,
    ) -> str:
        ranks = {"normal": 0, "warning": 1, "critical": 2, "down": 3}
        if ranks.get(severity, 0) > ranks.get(row["severity"], 0):
            return f"severidade subiu para {severity.upper()}"
        if m.loss >= self.s.down_loss and row["worst_loss"] < self.s.down_loss:
            return "destino chegou a 100% de perda"
        new_bucket = int(m.loss // self.s.loss_step) * int(self.s.loss_step)
        if loss_bad and new_bucket > row["notified_loss_bucket"]:
            return f"perda subiu para {new_bucket}%"
        if latency_bad and not row["notified_latency_loss"]:
            return "latência elevada surgiu no incidente"
        if jitter_bad and not row["notified_jitter"]:
            return "jitter elevado surgiu no incidente"
        if m.latency is not None and row["worst_latency"] is not None and m.latency >= row["worst_latency"] + self.s.latency_increase_ms:
            return f"latência aumentou mais {self.s.latency_increase_ms:.0f} ms"
        return ""

    @staticmethod
    def _next_reminder(started: float, now: float) -> float:
        elapsed = now - started
        if elapsed < 600:
            return started + 600
        if elapsed < 1200:
            return started + 1200
        if elapsed < 1800:
            return started + 1800
        if elapsed < 7200:
            return started + (math.floor(elapsed / 1800) + 1) * 1800
        return started + (math.floor(elapsed / 3600) + 1) * 3600

    def message(self, d: Decision) -> str:
        m = d.measurement
        if d.kind == "unavailable":
            header = "🔴 <b>DESTINO INDISPONÍVEL</b>"
        elif d.kind == "aggravated":
            header = "🔺 <b>INCIDENTE AGRAVADO</b>"
        elif d.kind == "persistent":
            header = "🟠 <b>INCIDENTE PERSISTENTE</b>"
        elif d.kind == "recovered":
            header = "✅ <b>DESTINO RECUPERADO</b>"
        else:
            latency_bad = bool(
                d.baseline and m.latency is not None
                and m.latency - d.baseline >= self.s.latency_increase_ms
                and 100 * (m.latency - d.baseline) / d.baseline >= self.s.latency_increase_percent
            )
            if d.severity == "critical":
                header = "🔴 <b>INCIDENTE CRÍTICO</b>"
            elif m.loss >= self.s.loss_threshold and latency_bad:
                header = "⚠️ <b>PERDA E LATÊNCIA ELEVADAS</b>"
            elif latency_bad:
                header = "⚠️ <b>LATÊNCIA ELEVADA</b>"
            else:
                header = "⚠️ <b>PERDA DE PACOTES</b>"
        latency_line = latency_detail(d.baseline, m.latency)
        if d.kind == "recovered":
            latency_line = f"📶 Latência atual: {fmt_latency(m.latency)}"
        severity_labels = {"warning": "AVISO", "critical": "CRÍTICO", "down": "INDISPONÍVEL"}
        lines = [
            header,
            f"Nível: <b>{severity_labels.get(d.severity, d.severity.upper())}</b>",
            "",
            target_heading(m),
            "",
            latency_line,
            jitter_detail(d.jitter_baseline, m.jitter),
            f"📉 Perda: {m.loss:.1f}%",
        ]
        if d.kind in {"initial", "unavailable"}:
            lines.append(f"✅ Confirmado em {self.s.confirmations} avaliações")
            lines.append(f"🕐 Início: {datetime.now().strftime('%H:%M')}")
        elif d.kind == "aggravated":
            lines.append(f"🔺 Motivo: {html.escape(d.reason)}")
            lines.append(f"🕐 Incidente ativo há {duration_text(time.time() - (d.incident_started or time.time()))}")
        elif d.kind == "persistent":
            lines.append(f"🕐 Incidente ativo há {duration_text(time.time() - (d.incident_started or time.time()))}")
        elif d.kind == "recovered":
            lines[-1] = f"📉 Perda atual: {m.loss:.1f}%"
            lines.append(f"⏱️ Duração do incidente: {duration_text(time.time() - (d.incident_started or time.time()))}")
            lines.extend(["", f"Normalizado às {datetime.now().strftime('%H:%M')}."])
        link = self.dashboard_link(m)
        if link:
            lines.extend(["", f'📊 <a href="{html.escape(link, quote=True)}">Abrir no Grafana</a>'])
        return "\n".join(lines)

    def dashboard_link(self, m: Measurement) -> str:
        if not self.s.grafana_dashboard_url:
            return ""
        separator = "&" if "?" in self.s.grafana_dashboard_url else "?"
        return (
            f"{self.s.grafana_dashboard_url}{separator}"
            f"var-categoria={quote(m.category, safe='')}&var-target={quote(m.title, safe='')}"
        )

    def grouped_initial(self, decisions: list[Decision]) -> str:
        lines = [f"🚨 <b>ANOMALIA EM MÚLTIPLOS DESTINOS</b>", "", f"{len(decisions)} destinos afetados:", ""]
        for d in decisions:
            m = d.measurement
            status = "indisponível" if d.severity == "down" else f"{d.severity}, perda {m.loss:.1f}%"
            if m.latency is not None and d.baseline:
                pct = 100 * (m.latency - d.baseline) / d.baseline
                if pct >= self.s.latency_increase_percent:
                    status += f", latência {d.baseline:.0f}→{m.latency:.0f} ms"
            lines.append(f"• <b>{html.escape(m.title)}</b> — {status}")
        return "\n".join(lines)

    def cycle(self) -> None:
        now = time.time()
        measurements = self.prometheus.measurements()
        LOG.info("Evaluating %d enabled targets", len(measurements))
        decisions: list[Decision] = []
        self.store.begin()
        try:
            for measurement in measurements:
                decision = self.process(measurement, now)
                if decision:
                    decisions.append(decision)
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise

        initial = [d for d in decisions if d.kind in {"initial", "unavailable"}]
        if len(initial) >= self.s.group_threshold:
            self.telegram.send(self.grouped_initial(initial), event="group_initial")
        else:
            for decision in initial:
                self.telegram.send(self.message(decision), decision.measurement.host, decision.kind)
        for decision in decisions:
            if decision not in initial:
                self.telegram.send(self.message(decision), decision.measurement.host, decision.kind)
        for decision in decisions:
            if decision.kind in {"initial", "unavailable", "aggravated"}:
                self.mtr.submit(decision)

    def run(self) -> None:
        while not STOP.is_set():
            started = time.monotonic()
            try:
                self.cycle()
            except Exception:
                LOG.exception("Evaluation cycle failed")
            remaining = max(1, self.s.evaluation_interval - (time.monotonic() - started))
            STOP.wait(remaining)


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings()
    settings.validate()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    LOG.info("Starting: Prometheus=%s DB=%s dry_run=%s", settings.prometheus_url, settings.db_path, settings.dry_run)
    Engine(settings).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

