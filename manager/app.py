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
USERNAME = os.getenv("MANAGER_USERNAME", "")
PASSWORD = os.getenv("MANAGER_PASSWORD", "")


NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}"
DURATION_PATTERN = r"[1-9]\d*(?:ms|s|m)"
HOSTNAME_PATTERN = (
    r"(?=.{1,253}\.?$)"
    r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.?"
)


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
    links: list[str] = Field(default_factory=list)
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

    @field_validator("links")
    @classmethod
    def validate_links(cls, value: list[str]) -> list[str]:
        names = [clean_name(item, "Link") for item in value]
        return list(dict.fromkeys(names))

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
            if payload.links:
                raise HTTPException(status_code=422, detail="Links só podem ser usados com um roteador")
            return
        router = self._router_by_name(document, payload.router)
        if router is None:
            raise HTTPException(status_code=422, detail=f"Roteador {payload.router} não existe")
        links = {str(link.get("name")): link for link in router.get("links") or []}
        unknown = [name for name in payload.links if name not in links]
        if unknown:
            raise HTTPException(status_code=422, detail=f"Links inexistentes em {payload.router}: {', '.join(unknown)}")
        if self._network(payload.host, payload.network) == "ip6":
            used = payload.links or list(links)
            missing = [name for name in used if not links[name].get("source6")]
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
            if payload.links:
                target["links"] = list(payload.links)
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
            "links": [str(link) for link in target.get("links") or []],
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
            kept_links = {link.name for link in payload.links}
            users = [t for t in document["targets"] if str(t.get("router") or "") == name]
            orphaned = sorted(
                {str(link) for t in users for link in t.get("links") or [] if str(link) not in kept_links}
            )
            if orphaned:
                raise HTTPException(
                    status_code=409,
                    detail=f"Links ainda usados por destinos: {', '.join(orphaned)}",
                )
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

    def metrics(self) -> tuple[dict[tuple[str, str, str, bool], dict[str, Any]], str]:
        """Metrics per target, keyed by (host, title, category, remote).

        Remote (router) series carry a ``link`` label, one series per uplink;
        they are aggregated per target and also returned per link, so a bad
        uplink isn't hidden by a healthy one.
        """
        group = "host,title,category,link"
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

        grouped: dict[tuple[str, str, str, bool], list[tuple[str, float, float, float, float]]] = {}
        for key, samples in sent_values.items():
            host, title, category, link = key
            jitter = 1000 * max(0.0, p95_values.get(key, 0) - p50_values.get(key, 0))
            grouped.setdefault((host, title, category, bool(link)), []).append(
                (link, samples, received_values.get(key, 0), duration_values.get(key, 0), jitter)
            )

        output: dict[tuple[str, str, str, bool], dict[str, Any]] = {}
        for key, rows in grouped.items():
            values = self._summary(
                sum(r[1] for r in rows), sum(r[2] for r in rows), sum(r[3] for r in rows), max(r[4] for r in rows)
            )
            if key[3]:
                values["links"] = [
                    {"name": link, **self._summary(sent, replies, seconds, jitter)}
                    for link, sent, replies, seconds, jitter in sorted(rows)
                ]
            output[key] = values
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
        key = (target["host"], target["title"], target["category"], bool(target["router"]))
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
    # A remote target is as bad as its worst uplink.
    loss = max([metrics["loss"], *(link["loss"] for link in metrics.get("links", []))])
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
