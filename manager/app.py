from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import shutil
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, field_validator, model_validator
from ruamel.yaml import YAML
from ruamel.yaml.scalarint import HexInt


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
# Chart periods: (range in seconds, step in seconds, increase/rate window).
# The window must cover at least one run of remote targets (1 per minute by
# default) and a few 15s scrapes, or the chart turns into gaps.
HISTORY_RANGES = {"1h": (3600, 60, "2m"), "6h": (21600, 300, "5m"), "24h": (86400, 900, "15m")}
USERNAME = os.getenv("MANAGER_USERNAME", "")
PASSWORD = os.getenv("MANAGER_PASSWORD", "")
WEBHOOK_PATH = Path(os.getenv("WEBHOOK_PATH", "/config/webhook.json"))
WEBHOOK_CHECK_SECONDS = 15


NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}"
DURATION_PATTERN = r"[1-9]\d*(?:ms|s|m)"
HOSTNAME_PATTERN = (
    r"(?=.{1,253}\.?$)"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.?"
)


def promql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def target_id(target: dict[str, Any]) -> str:
    labels = target.get("labels") or {}
    parts = [str(labels.get("category", "")), str(labels.get("title", "")), str(target.get("host", ""))]
    # Only remote targets carry the router in the hash, so ids of local
    # targets stay the same as before routers existed.
    if target.get("router"):
        parts.append(str(target["router"]))
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


def bool_label(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def tos_text(value: Any) -> str:
    # ruamel loads a bare 0x00 as HexInt, whose str() is "0".
    if isinstance(value, int):
        return f"0x{value:02x}"
    return str(value).strip("'\"")


def clean_host(value: str) -> str:
    value = value.strip()
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        if not re.fullmatch(HOSTNAME_PATTERN, value):
            raise ValueError("Informe um IPv4, IPv6 ou hostname válido")
        return value.rstrip(".")


def clean_name(value: str, what: str) -> str:
    value = value.strip()
    if not re.fullmatch(NAME_PATTERN, value):
        raise ValueError(f"{what} aceita apenas letras, números, '.', '_' e '-'")
    return value


def clean_duration(value: str) -> str:
    value = value.strip()
    if value and not re.fullmatch(DURATION_PATTERN, value):
        raise ValueError("Use uma duração como 500ms, 1s ou 1m")
    return value


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
    # Remote ping through a router (empty router = local ICMP from the prober).
    router: str = ""
    count: int | None = Field(default=None, ge=1, le=100)
    packet_interval: str = ""
    timeout: str = ""

    @field_validator("title", "category", "menu", "smokeping_name")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return " ".join(value.strip().split())

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        return clean_host(value)

    @field_validator("router")
    @classmethod
    def validate_router(cls, value: str) -> str:
        return clean_name(value, "Roteador") if value.strip() else ""

    @field_validator("packet_interval", "timeout")
    @classmethod
    def validate_remote_duration(cls, value: str) -> str:
        return clean_duration(value)

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
        if not re.fullmatch(DURATION_PATTERN, value.strip()):
            raise ValueError("Use um intervalo como 500ms, 1s ou 1m")
        return value.strip()

    @field_validator("tos")
    @classmethod
    def validate_tos(cls, value: str) -> str:
        if not re.fullmatch(r"0x[0-9a-fA-F]{2}", value.strip()):
            raise ValueError("TOS deve estar no formato 0x00")
        return value.lower()


class LinkInput(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    vpn_instance: str = Field(default="", max_length=31)
    source: str = ""
    source6: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return clean_name(value, "Nome do link")

    @field_validator("vpn_instance")
    @classmethod
    def validate_vpn_instance(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"\S+", value):
            raise ValueError("VPN instance não pode conter espaços")
        return value

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        value = value.strip()
        if value and not isinstance(_ip_or_none(value), ipaddress.IPv4Address):
            raise ValueError("source deve ser um endereço IPv4")
        return value

    @field_validator("source6")
    @classmethod
    def validate_source6(cls, value: str) -> str:
        value = value.strip()
        if value and not isinstance(_ip_or_none(value), ipaddress.IPv6Address):
            raise ValueError("source6 deve ser um endereço IPv6")
        return value

    @model_validator(mode="after")
    def require_source(self) -> "LinkInput":
        if not self.source and not self.source6:
            raise ValueError(f"Link {self.name}: informe source e/ou source6")
        return self


class RouterInput(BaseModel):
    name: str = Field(min_length=1, max_length=63)
    address: str = Field(min_length=1, max_length=260)
    username: str = Field(min_length=1, max_length=64)
    password_file: str = ""
    private_key_file: str = ""
    known_hosts: str = ""
    insecure_skip_host_key: bool = False
    sessions: int = Field(default=5, ge=1, le=20)
    links: list[LinkInput] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return clean_name(value, "Nome do roteador")

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"\S+", value):
            raise ValueError("Usuário não pode conter espaços")
        return value

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        value = value.strip()
        match = re.fullmatch(r"\[([0-9a-fA-F:.]+)\](?::(\d+))?", value)
        if match:
            host, port = match.group(1), match.group(2)
            if not isinstance(_ip_or_none(host), ipaddress.IPv6Address):
                raise ValueError("Endereço IPv6 inválido")
            host = f"[{host}]"
        elif value.count(":") > 1:
            raise ValueError("Use [IPv6]:porta para endereços IPv6")
        else:
            host, _, port = value.partition(":")
            host = clean_host(host)
        port = port or "22"
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("Porta SSH inválida")
        return f"{host}:{int(port)}"

    @field_validator("password_file", "private_key_file", "known_hosts")
    @classmethod
    def validate_path(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith("/"):
            raise ValueError("Use o caminho absoluto dentro do container do prober")
        return value

    @model_validator(mode="after")
    def validate_router(self) -> "RouterInput":
        if bool(self.password_file) == bool(self.private_key_file):
            raise ValueError("Informe password_file ou private_key_file (apenas um)")
        if not self.known_hosts and not self.insecure_skip_host_key:
            raise ValueError("Informe known_hosts ou marque insecure_skip_host_key")
        names = [link.name for link in self.links]
        if len(names) != len(set(names)):
            raise ValueError("Os nomes dos links devem ser únicos")
        return self


def _ip_or_none(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


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
        if not isinstance(document.get("routers") or [], list):
            raise RuntimeError("config.yaml inválido: a chave routers deve ser uma lista")
        document.setdefault("targets", [])
        return document

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            targets = self._read()["targets"]
            return [self._serialize(item) for item in targets]

    def categories(self) -> list[str]:
        return sorted({item["category"] for item in self.list()}, key=str.casefold)

    def webhook_context(self) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """Targets (with all their labels) and routers by name, without SSH credentials."""
        with self.lock:
            document = self._read()
            targets = [
                {**self._serialize(item), "labels": {str(k): str(v) for k, v in (item.get("labels") or {}).items()}}
                for item in document["targets"]
            ]
            routers = {}
            for item in document.get("routers") or []:
                router = self._serialize_router(item, [])
                routers[router["name"]] = {"name": router["name"], "address": router["address"], "links": router["links"]}
            return targets, routers

    def create(self, payload: TargetInput) -> tuple[dict[str, Any], str]:
        with self.lock:
            document = self._read()
            self._check_router(document, payload)
            candidate = self._to_yaml(payload)
            self._ensure_unique(document["targets"], candidate)
            document["targets"].append(candidate)
            warning = self._commit(document)
            return self._serialize(candidate), warning

    def update(self, item_id: str, payload: TargetInput) -> tuple[dict[str, Any], str]:
        with self.lock:
            document = self._read()
            index = self._find(document["targets"], item_id)
            self._check_router(document, payload)
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
            if (
                target.get("host") == candidate["host"]
                and old_labels.get("category") == labels["category"]
                and (target.get("router") or "") == (candidate.get("router") or "")
            ):
                raise HTTPException(status_code=409, detail="Este host já existe na mesma categoria")

    def _check_router(self, document: Any, payload: TargetInput) -> None:
        if not payload.router:
            return
        router = self._router_by_name(document, payload.router)
        if router is None:
            raise HTTPException(status_code=422, detail=f"Roteador {payload.router} não existe")
        # The router pings every target from all of its links.
        if self._network(payload.host, payload.network) == "ip6":
            missing = [str(link.get("name")) for link in router.get("links") or [] if not link.get("source6")]
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail=f"Destino IPv6 exige source6 nos links: {', '.join(missing)}",
                )

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
        target: dict[str, Any] = {"host": payload.host}
        if payload.router:
            target["router"] = payload.router
        target.update(
            {
                "interval": payload.interval,
                "network": self._network(payload.host, payload.network),
                "protocol": payload.protocol,
                "size": payload.size,
                # HexInt dumps as a bare 0x00; a str would be quoted ('0x00').
                "tos": HexInt(int(payload.tos, 16), width=2),
            }
        )
        if payload.router:
            if payload.count is not None:
                target["count"] = payload.count
            if payload.packet_interval:
                target["packet_interval"] = payload.packet_interval
            if payload.timeout:
                target["timeout"] = payload.timeout
        target["labels"] = labels
        return target

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
            "tos": tos_text(target.get("tos", 0)),
            "router": str(target.get("router") or ""),
            "count": int(target["count"]) if target.get("count") is not None else None,
            "packet_interval": str(target.get("packet_interval") or ""),
            "timeout": str(target.get("timeout") or ""),
        }

    # --- routers -----------------------------------------------------------

    def routers(self) -> list[dict[str, Any]]:
        with self.lock:
            document = self._read()
            return [self._serialize_router(item, document["targets"]) for item in document.get("routers") or []]

    def create_router(self, payload: RouterInput) -> tuple[dict[str, Any], str]:
        with self.lock:
            document = self._read()
            if self._router_by_name(document, payload.name) is not None:
                raise HTTPException(status_code=409, detail=f"Já existe um roteador chamado {payload.name}")
            candidate = self._router_to_yaml(payload)
            self._routers_list(document).append(candidate)
            warning = self._commit(document)
            return self._serialize_router(candidate, document["targets"]), warning

    def update_router(self, name: str, payload: RouterInput) -> tuple[dict[str, Any], str]:
        with self.lock:
            document = self._read()
            routers = self._routers_list(document)
            index = self._find_router(routers, name)
            if payload.name != name and self._router_by_name(document, payload.name) is not None:
                raise HTTPException(status_code=409, detail=f"Já existe um roteador chamado {payload.name}")
            users = [t for t in document["targets"] if str(t.get("router") or "") == name]
            candidate = self._router_to_yaml(payload)
            routers[index] = candidate
            for target in users:
                target["router"] = payload.name
            warning = self._commit(document)
            return self._serialize_router(candidate, document["targets"]), warning

    def delete_router(self, name: str) -> str:
        with self.lock:
            document = self._read()
            routers = self._routers_list(document)
            index = self._find_router(routers, name)
            used = sum(1 for t in document["targets"] if str(t.get("router") or "") == name)
            if used:
                raise HTTPException(
                    status_code=409,
                    detail=f"{used} destino(s) ainda usam o roteador {name}",
                )
            routers.pop(index)
            if not routers:
                del document["routers"]
            return self._commit(document)

    @staticmethod
    def _router_by_name(document: Any, name: str) -> Any:
        for router in document.get("routers") or []:
            if str(router.get("name")) == name:
                return router
        return None

    @staticmethod
    def _find_router(routers: list[Any], name: str) -> int:
        for index, router in enumerate(routers):
            if str(router.get("name")) == name:
                return index
        raise HTTPException(status_code=404, detail="Roteador não encontrado")

    @staticmethod
    def _routers_list(document: Any) -> list[Any]:
        if document.get("routers") is None:
            # Keep routers above targets, as in the prober docs.
            if hasattr(document, "insert"):
                document.insert(0, "routers", [])
            else:
                items = list(document.items())
                document.clear()
                document["routers"] = []
                document.update(items)
        return document["routers"]

    @staticmethod
    def _router_to_yaml(payload: RouterInput) -> dict[str, Any]:
        router: dict[str, Any] = {
            "name": payload.name,
            "address": payload.address,
            "username": payload.username,
        }
        if payload.password_file:
            router["password_file"] = payload.password_file
        else:
            router["private_key_file"] = payload.private_key_file
        if payload.insecure_skip_host_key:
            router["insecure_skip_host_key"] = True
        else:
            router["known_hosts"] = payload.known_hosts
        router["sessions"] = payload.sessions
        links = []
        for link in payload.links:
            item: dict[str, Any] = {"name": link.name}
            for key in ("vpn_instance", "source", "source6"):
                if getattr(link, key):
                    item[key] = getattr(link, key)
            links.append(item)
        router["links"] = links
        return router

    @staticmethod
    def _serialize_router(router: dict[str, Any], targets: list[Any]) -> dict[str, Any]:
        name = str(router.get("name", ""))
        return {
            "name": name,
            "address": str(router.get("address", "")),
            "username": str(router.get("username", "")),
            "password_file": str(router.get("password_file") or ""),
            "private_key_file": str(router.get("private_key_file") or ""),
            "known_hosts": str(router.get("known_hosts") or ""),
            "insecure_skip_host_key": bool(router.get("insecure_skip_host_key", False)),
            "sessions": int(router.get("sessions", 5)),
            "links": [
                {
                    "name": str(link.get("name", "")),
                    "vpn_instance": str(link.get("vpn_instance") or ""),
                    "source": str(link.get("source") or ""),
                    "source6": str(link.get("source6") or ""),
                }
                for link in router.get("links") or []
            ],
            "targets": sum(1 for t in targets if str(t.get("router") or "") == name),
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
    def keyed(
        rows: list[dict[str, Any]], names: tuple[str, ...] = ("host", "title", "category", "link")
    ) -> dict[tuple[str, ...], float]:
        result: dict[tuple[str, ...], float] = {}
        for row in rows:
            labels = row.get("metric", {})
            key = tuple(labels.get(name, "") for name in names)
            try:
                result[key] = float(row["value"][1])
            except (KeyError, TypeError, ValueError):
                continue
        return result

    def metrics(self, window: str = METRIC_WINDOW) -> tuple[dict[tuple[str, str, str], dict[str, Any]], str]:
        """Metrics per target, keyed by (host, title, category).

        The top-level values come from the local ping (series with no ``link``
        label). A router target also has one series per uplink, returned
        separately in ``links``; when there is no local series the top-level
        values are None.
        """
        group = "host,title,category,link"
        sent = f"sum by ({group}) (increase(smokeping_requests_total[{window}]))"
        received = f"sum by ({group}) (increase(smokeping_response_duration_seconds_count[{window}]))"
        duration = f"sum by ({group}) (increase(smokeping_response_duration_seconds_sum[{window}]))"
        buckets = f"sum by (le,{group}) (rate(smokeping_response_duration_seconds_bucket[{window}]))"
        try:
            sent_values = self.keyed(self.query(sent))
            received_values = self.keyed(self.query(received))
            duration_values = self.keyed(self.query(duration))
            p50_values = self.keyed(self.query(f"histogram_quantile(0.50, {buckets})"))
            p95_values = self.keyed(self.query(f"histogram_quantile(0.95, {buckets})"))
        except Exception as exc:
            LOG.warning("Prometheus unavailable: %s", exc)
            return {}, str(exc)

        output: dict[tuple[str, str, str], dict[str, Any]] = {}
        for key, samples in sorted(sent_values.items()):
            host, title, category, link = key
            summary = self._summary(
                samples,
                received_values.get(key, 0),
                duration_values.get(key, 0),
                1000 * max(0.0, p95_values.get(key, 0) - p50_values.get(key, 0)),
            )
            values = output.setdefault(
                (host, title, category), {"samples": None, "loss": None, "latency": None, "jitter": None}
            )
            if link:
                values.setdefault("links", []).append({"name": link, **summary})
            else:
                values.update(summary)
        return output, ""

    @staticmethod
    def _summary(samples: float, replies: float, total_seconds: float, jitter: float) -> dict[str, float]:
        loss = max(0.0, min(100.0, 100 * (1 - replies / samples))) if samples > 0 else 0
        latency = 1000 * total_seconds / replies if replies > 0 else 0
        return {
            "samples": round(samples, 1),
            "loss": round(loss, 2),
            "latency": round(latency, 2),
            "jitter": round(jitter, 2),
        }

    def query_range(self, expression: str, start: float, end: float, step: int) -> list[dict[str, Any]]:
        response = self.http.get(
            f"{PROMETHEUS_URL}/api/v1/query_range",
            params={"query": expression, "start": start, "end": end, "step": step},
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("status") != "success":
            raise RuntimeError(body)
        return body["data"]["result"]

    @staticmethod
    def keyed_range(rows: list[dict[str, Any]]) -> dict[str, dict[float, float]]:
        """Matrix rows as {link: {timestamp: value}}, skipping NaN/Inf samples."""
        result: dict[str, dict[float, float]] = {}
        for row in rows:
            series = result.setdefault(row.get("metric", {}).get("link", ""), {})
            for timestamp, raw in row.get("values", []):
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    series[timestamp] = value
        return result

    def history(
        self, host: str, title: str, category: str, range_name: str, now: float | None = None
    ) -> list[dict[str, Any]]:
        """Loss/latency/jitter over time for one target, one series per origin.

        The local ping comes first (name ""), then one series per router link.
        Steps where nothing was sent are left out so the chart shows a gap.
        """
        if range_name not in HISTORY_RANGES:
            raise HTTPException(status_code=422, detail=f"Período inválido: {range_name}")
        seconds, step, window = HISTORY_RANGES[range_name]
        end = now if now is not None else datetime.now(timezone.utc).timestamp()
        start = end - seconds
        selector = ",".join(
            f'{name}="{promql_escape(value)}"'
            for name, value in (("host", host), ("title", title), ("category", category))
        )

        def run(expression: str) -> dict[str, dict[float, float]]:
            return self.keyed_range(self.query_range(expression, start, end, step))

        sent = run(f"sum by (link) (increase(smokeping_requests_total{{{selector}}}[{window}]))")
        received = run(f"sum by (link) (increase(smokeping_response_duration_seconds_count{{{selector}}}[{window}]))")
        duration = run(f"sum by (link) (increase(smokeping_response_duration_seconds_sum{{{selector}}}[{window}]))")
        buckets = f"sum by (le,link) (rate(smokeping_response_duration_seconds_bucket{{{selector}}}[{window}]))"
        p50 = run(f"histogram_quantile(0.50, {buckets})")
        p95 = run(f"histogram_quantile(0.95, {buckets})")

        def ms(value: float | None) -> float | None:
            return None if value is None else round(1000 * value, 2)

        series = []
        for link in sorted(sent):
            points = []
            for timestamp, samples in sorted(sent[link].items()):
                if samples <= 0:
                    continue
                replies = received.get(link, {}).get(timestamp, 0)
                low = p50.get(link, {}).get(timestamp) if replies > 0 else None
                high = p95.get(link, {}).get(timestamp) if replies > 0 else None
                points.append({
                    "t": timestamp,
                    "loss": round(max(0.0, min(100.0, 100 * (1 - replies / samples))), 2),
                    "latency": ms(duration.get(link, {}).get(timestamp, 0) / replies) if replies > 0 else None,
                    "p50": ms(low),
                    "p95": ms(high),
                    "jitter": ms(max(0.0, high - low)) if low is not None and high is not None else None,
                })
            series.append({"name": link, "points": points})
        return series

    def router_health(self) -> dict[str, dict[str, float]]:
        try:
            sessions = self.keyed(self.query("sum by (router) (smokeping_remote_sessions_up)"), ("router",))
            errors = self.keyed(
                self.query(f"sum by (router) (increase(smokeping_remote_errors_total[{METRIC_WINDOW}]))"),
                ("router",),
            )
        except Exception as exc:
            LOG.warning("Prometheus unavailable: %s", exc)
            return {}
        return {
            key[0]: {"sessions_up": sessions.get(key, 0), "errors": round(errors.get(key, 0), 1)}
            for key in set(sessions) | set(errors)
        }


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


class WebhookInput(BaseModel):
    enabled: bool = False
    url: str = Field(default="", max_length=2000)
    auth_header: str = Field(default="", max_length=100)
    # Empty on save keeps the stored value, so the UI never needs to read the secret back.
    auth_value: str = Field(default="", max_length=4000)
    loss_percent: float = Field(default=20, gt=0, le=100)
    duration_seconds: int = Field(default=60, ge=30, le=3600)
    send_recovery: bool = True
    recovery_loss_percent: float = Field(default=5, ge=0, le=100)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"https?://\S+", value):
            raise ValueError("A URL do webhook deve começar com http:// ou https://")
        return value

    @field_validator("auth_header")
    @classmethod
    def validate_header(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[A-Za-z0-9-]+", value):
            raise ValueError("Nome do header inválido")
        return value

    @model_validator(mode="after")
    def validate_webhook(self) -> "WebhookInput":
        if self.enabled and not self.url:
            raise ValueError("Informe a URL para ativar o webhook")
        if self.recovery_loss_percent >= self.loss_percent:
            raise ValueError("A perda de recuperação deve ser menor que a perda de disparo")
        return self


class WebhookSettings:
    """Webhook settings in a JSON file next to config.yaml (the prober never reads it)."""

    def __init__(self, path: Path = WEBHOOK_PATH):
        self.path = path
        self.lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return WebhookInput(**data).model_dump()
        except FileNotFoundError:
            return WebhookInput().model_dump()
        except Exception as exc:
            LOG.warning("Invalid %s, webhook disabled: %s", self.path, exc)
            return WebhookInput().model_dump()

    def merged(self, payload: WebhookInput) -> dict[str, Any]:
        data = payload.model_dump()
        if not data["auth_header"]:
            data["auth_value"] = ""
        elif not data["auth_value"]:
            data["auth_value"] = self.load()["auth_value"]
        return data

    def save(self, payload: WebhookInput) -> dict[str, Any]:
        with self.lock:
            data = self.merged(payload)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            # 0600: the file holds the webhook auth secret.
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
            os.replace(temporary, self.path)
            return data

    @staticmethod
    def public(data: dict[str, Any]) -> dict[str, Any]:
        return {**{k: v for k, v in data.items() if k != "auth_value"}, "auth_value_set": bool(data["auth_value"])}


def now_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def series_values(values: dict[str, Any]) -> dict[str, Any]:
    lost = values["loss"] >= 100
    return {
        "samples": values["samples"],
        "loss_percent": values["loss"],
        "latency_ms": None if lost else values["latency"],
        "jitter_ms": None if lost else values["jitter"],
    }


class WebhookWatcher:
    """Posts to the webhook when a series (local ping or one router link) has high loss.

    Loss is averaged over the last ``duration_seconds``; one ``loss_high`` per
    incident, then ``recovered`` once it drops below ``recovery_loss_percent``.
    A failed POST is retried on the next check. State lives in memory, so a
    restart forgets open incidents.
    """

    def __init__(self, store: ConfigStore, prometheus: PrometheusClient, settings: WebhookSettings):
        self.store = store
        self.prometheus = prometheus
        self.settings = settings
        self.open: dict[tuple[str, str, str, str], float] = {}

    def check(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        settings = self.settings.load()
        if not settings["enabled"]:
            self.open.clear()
            return
        metrics, error = self.prometheus.metrics(f"{settings['duration_seconds']}s")
        if error:
            return
        targets, routers = self.store.webhook_context()
        seen = set()
        for target in targets:
            values = metrics.get((target["host"], target["title"], target["category"]))
            if not values:
                continue
            for link, series in self._series(values):
                if not series["samples"]:
                    continue  # no ping run inside the window: keep the current state
                key = (target["host"], target["title"], target["category"], link)
                seen.add(key)
                if key not in self.open and series["loss"] >= settings["loss_percent"]:
                    payload = build_payload("loss_high", target, routers, link, values, settings, now, now)
                    if send_webhook(settings, payload):
                        self.open[key] = now
                elif key in self.open and series["loss"] < settings["recovery_loss_percent"]:
                    started = self.open[key]
                    payload = build_payload("recovered", target, routers, link, values, settings, now, started)
                    if not settings["send_recovery"] or send_webhook(settings, payload):
                        del self.open[key]
        # Forget incidents of targets that were deleted or renamed.
        for key in set(self.open) - seen:
            if not any(key[:3] == (t["host"], t["title"], t["category"]) for t in targets):
                del self.open[key]

    @staticmethod
    def _series(values: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        local = [("", values)] if values.get("samples") is not None else []
        return local + [(link["name"], link) for link in values.get("links", [])]

    def run(self, stop: threading.Event) -> None:
        while not stop.wait(WEBHOOK_CHECK_SECONDS):
            try:
                self.check()
            except Exception:
                LOG.exception("Webhook check failed")


def build_payload(
    event: str,
    target: dict[str, Any],
    routers: dict[str, dict[str, Any]],
    link: str,
    values: dict[str, Any] | None,
    settings: dict[str, Any],
    now: float,
    started: float,
) -> dict[str, Any]:
    router = routers.get(target["router"]) if target["router"] else None
    link_info = {}
    if router:
        link_info = next((item for item in router["links"] if item["name"] == link), {})
    if link:
        probe = {
            "type": "remote",
            "router": {"name": target["router"], "address": router["address"] if router else ""},
            "link": link_info or {"name": link},
        }
    else:
        probe = {"type": "local", "router": None, "link": None}

    all_series = []
    failing = None
    for name, series in WebhookWatcher._series(values or {}):
        info = next((item for item in router["links"] if item["name"] == name), {}) if router and name else {}
        entry = {
            "link": name or None,
            "vpn_instance": info.get("vpn_instance") or None,
            "source": info.get("source") or None,
            "source6": info.get("source6") or None,
            **series_values(series),
        }
        entry["failing"] = entry["loss_percent"] >= settings["loss_percent"]
        all_series.append(entry)
        if name == link:
            failing = series_values(series)

    return {
        "event": event,
        "timestamp": now_iso(now),
        "incident": {"started_at": now_iso(started), "duration_seconds": round(now - started)},
        "rule": {
            "loss_percent": settings["loss_percent"],
            "window_seconds": settings["duration_seconds"],
            "recovery_loss_percent": settings["recovery_loss_percent"],
        },
        "target": target,
        "probe": probe,
        "metrics": failing,
        "all_series": all_series,
    }


def send_webhook(settings: dict[str, Any], payload: dict[str, Any]) -> bool:
    headers = {settings["auth_header"]: settings["auth_value"]} if settings["auth_header"] else {}
    try:
        response = requests.post(settings["url"], json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        return True
    except Exception as exc:
        LOG.warning("Webhook %s failed: %s", payload["event"], exc)
        return False


store = ConfigStore()
prometheus = PrometheusClient()
webhook_settings = WebhookSettings()
webhook_watcher = WebhookWatcher(store, prometheus, webhook_settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    stop = threading.Event()
    threading.Thread(target=webhook_watcher.run, args=(stop,), name="webhook", daemon=True).start()
    yield
    stop.set()


security = HTTPBasic(auto_error=False)
templates = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape())
app = FastAPI(title="SmokePing Manager", version="1.0.0", docs_url=None, redoc_url=None, lifespan=lifespan)
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


def metric_status(metrics: dict[str, Any] | None) -> str:
    if not metrics:
        return "unknown"
    # A router target is as bad as the worst of its local ping and uplinks.
    losses = [metrics["loss"], *(link["loss"] for link in metrics.get("links", []))]
    loss = max(value for value in losses if value is not None)
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


@app.get("/api/targets/{item_id}/history", dependencies=[Depends(authenticate)])
def target_history(item_id: str, range: str = "1h") -> dict[str, Any]:
    target = next((item for item in store.list() if item["id"] == item_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="Destino não encontrado")
    try:
        series = prometheus.history(target["host"], target["title"], target["category"], range)
    except HTTPException:
        raise
    except Exception as exc:
        LOG.warning("Prometheus unavailable: %s", exc)
        raise HTTPException(status_code=502, detail=f"Prometheus indisponível: {exc}")
    return {"range": range, "step": HISTORY_RANGES[range][1], "series": series}


@app.get("/api/routers", dependencies=[Depends(authenticate)])
def list_routers() -> dict[str, Any]:
    routers = store.routers()
    health = prometheus.router_health()
    for router in routers:
        router["health"] = health.get(router["name"])
    return {"routers": routers}


@app.post("/api/routers", status_code=201, dependencies=[Depends(authenticate)])
def create_router(payload: RouterInput) -> dict[str, Any]:
    router, warning = store.create_router(payload)
    return {"router": router, "warning": warning}


@app.put("/api/routers/{name}", dependencies=[Depends(authenticate)])
def update_router(name: str, payload: RouterInput) -> dict[str, Any]:
    router, warning = store.update_router(name, payload)
    return {"router": router, "warning": warning}


@app.delete("/api/routers/{name}", dependencies=[Depends(authenticate)])
def delete_router(name: str) -> dict[str, str]:
    warning = store.delete_router(name)
    return {"status": "deleted", "warning": warning}


@app.post("/api/reload", dependencies=[Depends(authenticate)])
def force_reload() -> dict[str, str]:
    warning = reload_prober()
    if warning:
        raise HTTPException(status_code=502, detail=warning)
    return {"status": "reloaded"}


@app.get("/api/webhook", dependencies=[Depends(authenticate)])
def get_webhook() -> dict[str, Any]:
    return WebhookSettings.public(webhook_settings.load())


@app.put("/api/webhook", dependencies=[Depends(authenticate)])
def save_webhook(payload: WebhookInput) -> dict[str, Any]:
    return WebhookSettings.public(webhook_settings.save(payload))


@app.post("/api/webhook/test", dependencies=[Depends(authenticate)])
def test_webhook(payload: WebhookInput) -> dict[str, Any]:
    """Sends a sample event built from a real target (a remote one when there is one)."""
    settings = webhook_settings.merged(payload)
    if not settings["url"]:
        raise HTTPException(status_code=422, detail="Informe a URL do webhook")
    targets, routers = store.webhook_context()
    if not targets:
        raise HTTPException(status_code=422, detail="Cadastre um destino antes de testar")
    target = next((t for t in targets if t["router"] in routers), targets[0])
    metrics, _ = prometheus.metrics(f"{settings['duration_seconds']}s")
    values = metrics.get((target["host"], target["title"], target["category"]))
    links = routers[target["router"]]["links"] if target["router"] in routers else []
    now = time.time()
    body = build_payload("test", target, routers, links[0]["name"] if links else "", values, settings, now, now)
    headers = {settings["auth_header"]: settings["auth_value"]} if settings["auth_header"] else {}
    try:
        response = requests.post(settings["url"], json=body, headers=headers, timeout=10)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Webhook inacessível: {exc}")
    if not response.ok:
        raise HTTPException(status_code=502, detail=f"Webhook respondeu HTTP {response.status_code}")
    return {"status": response.status_code, "payload": body}
