from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, field_validator
from ruamel.yaml import YAML


LOG = logging.getLogger("smokeping-manager")
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/config.yaml"))
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/config/backups"))
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus-smokeping:9090").rstrip("/")
PROBER_RELOAD_URL = os.getenv(
    "PROBER_RELOAD_URL", "http://smokeping-prober:9374/-/reload"
)
RELOAD_REQUIRED = os.getenv("RELOAD_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}
METRIC_WINDOW = os.getenv("METRIC_WINDOW", "5m")
WARNING_LOSS = float(os.getenv("WARNING_LOSS_PERCENT", "5"))
CRITICAL_LOSS = float(os.getenv("CRITICAL_LOSS_PERCENT", "20"))
DOWN_LOSS = float(os.getenv("DOWN_LOSS_PERCENT", "99.9"))
USERNAME = os.getenv("MANAGER_USERNAME", "")
PASSWORD = os.getenv("MANAGER_PASSWORD", "")


def target_id(target: dict[str, Any]) -> str:
    labels = target.get("labels") or {}
    raw = "\0".join(
        [str(labels.get("category", "")), str(labels.get("title", "")), str(target.get("host", ""))]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def bool_label(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class TargetInput(BaseModel):
    title: str = Field(min_length=1, max_length=180)
    host: str = Field(min_length=1, max_length=253)
    category: str = Field(min_length=1, max_length=80)
    menu: str = Field(default="", max_length=100)
    smokeping_name: str = Field(default="", max_length=120)
    network: str = "auto"
    protocol: str = "icmp"
    interval: str = "1s"
    size: int = Field(default=56, ge=8, le=9000)
    tos: str = "0x00"
    alerts_enabled: bool = True

    @field_validator("title", "category", "menu", "smokeping_name")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return " ".join(value.strip().split())

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        value = value.strip()
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            hostname_pattern = (
                r"(?=.{1,253}\.?$)"
                r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
                r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.?"
            )
            if not re.fullmatch(hostname_pattern, value):
                raise ValueError("Informe um IPv4, IPv6 ou hostname válido")
            return value.rstrip(".")

    @field_validator("network")
    @classmethod
    def validate_network(cls, value: str) -> str:
        if value not in {"auto", "ip4", "ip6"}:
            raise ValueError("network deve ser auto, ip4 ou ip6")
        return value

    @field_validator("protocol")
    @classmethod
    def validate_protocol(cls, value: str) -> str:
        if value != "icmp":
            raise ValueError("A interface atualmente suporta somente ICMP")
        return value

    @field_validator("interval")
    @classmethod
    def validate_interval(cls, value: str) -> str:
        if not re.fullmatch(r"[1-9]\d*(?:ms|s|m)", value.strip()):
            raise ValueError("Use um intervalo como 500ms, 1s ou 1m")
        return value.strip()

    @field_validator("tos")
    @classmethod
    def validate_tos(cls, value: str) -> str:
        if not re.fullmatch(r"0x[0-9a-fA-F]{2}", value.strip()):
            raise ValueError("TOS deve estar no formato 0x00")
        return value.lower()


class ConfigStore:
    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.yaml = YAML()
        self.yaml.preserve_quotes = True
        self.yaml.indent(mapping=2, sequence=4, offset=2)

    def _read(self) -> Any:
        if not self.path.exists():
            return {"targets": []}
        with self.path.open("r", encoding="utf-8") as stream:
            document = self.yaml.load(stream) or {}
        if not isinstance(document, dict) or not isinstance(document.get("targets", []), list):
            raise RuntimeError("config.yaml inválido: a chave targets deve ser uma lista")
        document.setdefault("targets", [])
        return document

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            targets = self._read()["targets"]
            return [self._serialize(item) for item in targets]

    def categories(self) -> list[str]:
        return sorted({item["category"] for item in self.list()}, key=str.casefold)

    def create(self, payload: TargetInput) -> tuple[dict[str, Any], str]:
        with self.lock:
            document = self._read()
            candidate = self._to_yaml(payload)
            self._ensure_unique(document["targets"], candidate)
            document["targets"].append(candidate)
            warning = self._commit(document)
            return self._serialize(candidate), warning

    def update(self, item_id: str, payload: TargetInput) -> tuple[dict[str, Any], str]:
        with self.lock:
            document = self._read()
            index = self._find(document["targets"], item_id)
            candidate = self._to_yaml(payload)
            self._ensure_unique(document["targets"], candidate, ignore=index)
            document["targets"][index] = candidate
            warning = self._commit(document)
            return self._serialize(candidate), warning

    def toggle(self, item_id: str, enabled: bool) -> tuple[dict[str, Any], str]:
        with self.lock:
            document = self._read()
            index = self._find(document["targets"], item_id)
            target = document["targets"][index]
            target.setdefault("labels", {})["alerts_enabled"] = "true" if enabled else "false"
            warning = self._commit(document)
            return self._serialize(target), warning

    def delete(self, item_id: str) -> str:
        with self.lock:
            document = self._read()
            index = self._find(document["targets"], item_id)
            document["targets"].pop(index)
            return self._commit(document)

    @staticmethod
    def _find(targets: list[Any], item_id: str) -> int:
        for index, target in enumerate(targets):
            if target_id(target) == item_id:
                return index
        raise HTTPException(status_code=404, detail="Destino não encontrado")

    @staticmethod
    def _ensure_unique(targets: list[Any], candidate: dict[str, Any], ignore: int | None = None) -> None:
        labels = candidate["labels"]
        for index, target in enumerate(targets):
            if index == ignore:
                continue
            old_labels = target.get("labels") or {}
            if target.get("host") == candidate["host"] and old_labels.get("category") == labels["category"]:
                raise HTTPException(status_code=409, detail="Este host já existe na mesma categoria")

    @staticmethod
    def _network(host: str, requested: str) -> str:
        if requested != "auto":
            return requested
        try:
            return "ip6" if ipaddress.ip_address(host).version == 6 else "ip4"
        except ValueError:
            # Hostnames use IPv4 by default; the operator can explicitly
            # select ip6 when the name must resolve only through AAAA.
            return "ip4"

    def _to_yaml(self, payload: TargetInput) -> dict[str, Any]:
        labels = {
            "category": payload.category,
            "menu": payload.menu or payload.title,
            "title": payload.title,
            "smokeping_name": payload.smokeping_name or self._slug(payload.title, payload.host),
            "alerts_enabled": "true" if payload.alerts_enabled else "false",
        }
        return {
            "host": payload.host,
            "interval": payload.interval,
            "network": self._network(payload.host, payload.network),
            "protocol": payload.protocol,
            "size": payload.size,
            "tos": payload.tos,
            "labels": labels,
        }

    @staticmethod
    def _slug(title: str, host: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")[:70]
        suffix = hashlib.sha1(host.encode()).hexdigest()[:6]
        return f"{value or 'target'}-{suffix}"

    @staticmethod
    def _serialize(target: dict[str, Any]) -> dict[str, Any]:
        labels = target.get("labels") or {}
        return {
            "id": target_id(target),
            "host": str(target.get("host", "")),
            "title": str(labels.get("title", target.get("host", ""))),
            "category": str(labels.get("category", "Sem categoria")),
            "menu": str(labels.get("menu", "")),
            "smokeping_name": str(labels.get("smokeping_name", "")),
            "alerts_enabled": bool_label(labels.get("alerts_enabled", "false")),
            "network": str(target.get("network", "auto")),
            "protocol": str(target.get("protocol", "icmp")),
            "interval": str(target.get("interval", "1s")),
            "size": int(target.get("size", 56)),
            "tos": str(target.get("tos", "0x00")),
        }

    def _commit(self, document: Any) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup: Path | None = None
        if self.path.exists():
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backup = BACKUP_DIR / f"config-{timestamp}.yaml"
            shutil.copy2(self.path, backup)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                self.yaml.dump(document, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            warning = reload_prober()
            if warning and RELOAD_REQUIRED:
                if backup:
                    shutil.copy2(backup, self.path)
                raise HTTPException(status_code=502, detail=f"Reload falhou; configuração restaurada: {warning}")
            self._prune_backups(30)
            return warning
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _prune_backups(keep: int) -> None:
        backups = sorted(BACKUP_DIR.glob("config-*.yaml"), reverse=True)
        for old in backups[keep:]:
            old.unlink(missing_ok=True)


class PrometheusClient:
    def __init__(self):
        self.http = requests.Session()

    def query(self, expression: str) -> list[dict[str, Any]]:
        response = self.http.get(
            f"{PROMETHEUS_URL}/api/v1/query", params={"query": expression}, timeout=8
        )
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "success":
            raise RuntimeError(body)
        return body["data"]["result"]

    @staticmethod
    def keyed(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], float]:
        result: dict[tuple[str, str, str], float] = {}
        for row in rows:
            labels = row.get("metric", {})
            key = (labels.get("host", ""), labels.get("title", ""), labels.get("category", ""))
            try:
                result[key] = float(row["value"][1])
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def metrics(self) -> tuple[dict[tuple[str, str, str], dict[str, float]], str]:
        group = "host,title,category"
        sent = f"sum by ({group}) (increase(smokeping_requests_total[{METRIC_WINDOW}]))"
        received = f"sum by ({group}) (increase(smokeping_response_duration_seconds_count[{METRIC_WINDOW}]))"
        duration = f"sum by ({group}) (increase(smokeping_response_duration_seconds_sum[{METRIC_WINDOW}]))"
        buckets = f"sum by (le,{group}) (rate(smokeping_response_duration_seconds_bucket[{METRIC_WINDOW}]))"
        try:
            sent_values = self.keyed(self.query(sent))
            received_values = self.keyed(self.query(received))
            duration_values = self.keyed(self.query(duration))
            p50_values = self.keyed(self.query(f"histogram_quantile(0.50, {buckets})"))
            p95_values = self.keyed(self.query(f"histogram_quantile(0.95, {buckets})"))
        except Exception as exc:
            LOG.warning("Prometheus unavailable: %s", exc)
            return {}, str(exc)

        output: dict[tuple[str, str, str], dict[str, float]] = {}
        for key, samples in sent_values.items():
            replies = received_values.get(key, 0)
            total_seconds = duration_values.get(key, 0)
            loss = max(0.0, min(100.0, 100 * (1 - replies / samples))) if samples > 0 else 0
            latency = 1000 * total_seconds / replies if replies > 0 else 0
            jitter = 1000 * max(0.0, p95_values.get(key, 0) - p50_values.get(key, 0))
            output[key] = {
                "samples": round(samples, 1),
                "loss": round(loss, 2),
                "latency": round(latency, 2),
                "jitter": round(jitter, 2),
            }
        return output, ""


def reload_prober() -> str:
    if not PROBER_RELOAD_URL:
        return ""
    try:
        response = requests.post(PROBER_RELOAD_URL, timeout=8)
        response.raise_for_status()
        return ""
    except Exception as exc:
        LOG.warning("Could not reload prober: %s", exc)
        return str(exc)


store = ConfigStore()
prometheus = PrometheusClient()
security = HTTPBasic(auto_error=False)
templates = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
app = FastAPI(title="SmokePing Manager", version="1.0.0", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def authenticate(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    if not USERNAME and not PASSWORD:
        return
    import secrets

    valid_user = credentials and secrets.compare_digest(credentials.username, USERNAME)
    valid_password = credentials and secrets.compare_digest(credentials.password, PASSWORD)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticação necessária",
            headers={"WWW-Authenticate": "Basic realm=SmokePing Manager"},
        )


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(authenticate)])
def index(request: Request) -> HTMLResponse:
    template = templates.get_template("index.html")
    return HTMLResponse(template.render(request=request, metric_window=METRIC_WINDOW))


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/targets", dependencies=[Depends(authenticate)])
def list_targets() -> dict[str, Any]:
    targets = store.list()
    metric_values, metric_error = prometheus.metrics()
    for target in targets:
        key = (target["host"], target["title"], target["category"])
        target["metrics"] = metric_values.get(key)
        target["status"] = metric_status(target["metrics"])
    return {
        "targets": targets,
        "categories": sorted({target["category"] for target in targets}, key=str.casefold),
        "metric_error": metric_error,
        "window": METRIC_WINDOW,
    }


def metric_status(metrics: dict[str, float] | None) -> str:
    if not metrics:
        return "unknown"
    loss = metrics["loss"]
    if loss >= DOWN_LOSS:
        return "down"
    if loss >= CRITICAL_LOSS:
        return "critical"
    if loss >= WARNING_LOSS:
        return "warning"
    return "healthy"


@app.post("/api/targets", status_code=201, dependencies=[Depends(authenticate)])
def create_target(payload: TargetInput) -> dict[str, Any]:
    target, warning = store.create(payload)
    return {"target": target, "warning": warning}


@app.put("/api/targets/{item_id}", dependencies=[Depends(authenticate)])
def update_target(item_id: str, payload: TargetInput) -> dict[str, Any]:
    target, warning = store.update(item_id, payload)
    return {"target": target, "warning": warning}


class ToggleInput(BaseModel):
    enabled: bool


@app.patch("/api/targets/{item_id}/alerts", dependencies=[Depends(authenticate)])
def toggle_alert(item_id: str, payload: ToggleInput) -> dict[str, Any]:
    target, warning = store.toggle(item_id, payload.enabled)
    return {"target": target, "warning": warning}


@app.delete("/api/targets/{item_id}", dependencies=[Depends(authenticate)])
def delete_target(item_id: str) -> dict[str, str]:
    warning = store.delete(item_id)
    return {"status": "deleted", "warning": warning}


@app.post("/api/reload", dependencies=[Depends(authenticate)])
def force_reload() -> dict[str, str]:
    warning = reload_prober()
    if warning:
        raise HTTPException(status_code=502, detail=warning)
    return {"status": "reloaded"}
