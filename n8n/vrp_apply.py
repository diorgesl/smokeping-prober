#!/usr/bin/env python3
"""Aplica comandos VRP num roteador Huawei (system-view + commit) para o workflow do n8n.

Uso: vrp_apply.py <roteador> <comandos em base64, um por linha>

O acesso aos roteadores fica em routers.json, ao lado deste arquivo:
  {"ne8k": {"host": "10.0.0.1", "port": 22, "username": "n8n-bgp", "password": "..."}}

Só aceita os comandos que o workflow gera; qualquer outro é recusado antes de conectar.
"""
import base64
import json
import re
import sys
from pathlib import Path

from netmiko import ConnectHandler

ROUTERS = Path(__file__).with_name("routers.json")
ALLOWED = re.compile(
    r"bgp \d+"
    r"|ipv4-family (unicast|vpn-instance [\w.-]+)"
    r"|(undo )?peer [0-9a-fA-F.:]+ ignore"
    r"|peer [0-9a-fA-F.:]+ route-policy [\w.-]+ export"
)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    name, blob = sys.argv[1], sys.argv[2]
    commands = base64.b64decode(blob, validate=True).decode().splitlines()
    for command in commands:
        if not ALLOWED.fullmatch(command):
            print(f"comando recusado: {command!r}", file=sys.stderr)
            return 3
    routers = json.loads(ROUTERS.read_text())
    if name not in routers:
        print(f"roteador {name!r} não está em {ROUTERS}", file=sys.stderr)
        return 4
    with ConnectHandler(device_type="huawei_vrpv8", **routers[name]) as conn:
        # NE8000 usa configuração em dois estágios: nada vale sem o commit.
        output = conn.send_config_set(commands, exit_config_mode=False)
        output += conn.commit()
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
