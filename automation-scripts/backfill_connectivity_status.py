#!/usr/bin/env python3
"""
backfill_connectivity_status.py
================================
Migração única: roda a checagem rápida de conectividade (mesma usada
pelos badges SSH/SNMP estilo Zabbix do dashboard, ver
test_ssh_connectivity()/test_snmp_connectivity() em discovery_core.py)
em TODO device que já tem credencial configurada, mas que ainda nunca
foi testado (discovery_ssh_status/discovery_snmp_status vazios).

Sem rodar isso, um device cadastrado ANTES dessa funcionalidade existir
fica com os badges cinza pra sempre, mesmo já tendo credencial completa
e funcionando -- o teste só dispara sozinho quando o operador edita a
credencial daquele protocolo de novo (ver app.py:_apply_discovery_form).
Este script varre todos de uma vez e resolve isso sem precisar reabrir/
resalvar cada device manualmente.

Idempotente: por padrão só testa quem ainda está sem status (nunca
testado) -- um device já testado antes não é testado de novo, a menos
que --force seja usado.

Uso:
    python backfill_connectivity_status.py            # só quem nunca foi testado
    python backfill_connectivity_status.py --dry-run  # só lista quem seria testado
    python backfill_connectivity_status.py --force    # retesta TODOS que têm credencial

Mesma lógica de resolução de URL/token de encrypt_existing_secrets.py
(prioriza discovery_output/_oracle/settings.json, cai pro .env se não
existir) -- e pelo mesmo motivo desse script (decrypt_secret() precisa
da MESMA chave usada pra cifrar), rode dentro do container netbox-oracle:

    docker compose exec netbox-oracle python backfill_connectivity_status.py
"""

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

import discovery_core as core

load_dotenv()

_OUTPUT_DIR = Path(__file__).parent / "discovery_output"
_SETTINGS_PATH = _OUTPUT_DIR / "_oracle" / "settings.json"


def _load_netbox_url_token():
    env_url = os.environ.get("NETBOX_URL") or ""
    env_token = os.environ.get("NETBOX_TOKEN") or ""
    if _SETTINGS_PATH.exists():
        try:
            data = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
            return (data.get("netbox_url") or env_url, data.get("netbox_token") or env_token)
        except Exception:
            pass
    return env_url, env_token


def main():
    parser = argparse.ArgumentParser(
        description="Roda a checagem de conectividade SSH/SNMP em devices que já têm credencial mas nunca foram testados."
    )
    parser.add_argument("--dry-run", action="store_true", help="Só lista quem seria testado, não grava nada")
    parser.add_argument("--force", action="store_true", help="Testa de novo mesmo quem já tem status gravado")
    args = parser.parse_args()

    url, token = _load_netbox_url_token()
    if not url or not token:
        print(
            "[erro] NetBox não configurado -- sem netbox_url/netbox_token em "
            f"{_SETTINGS_PATH} nem NETBOX_URL/NETBOX_TOKEN no ambiente."
        )
        return
    nb = core.get_client(url, token)
    devices = list(nb.dcim.devices.all())

    tested, skipped_no_cred, skipped_already_tested = 0, 0, 0
    for device in devices:
        cf = device.custom_fields or {}
        username = cf.get("discovery_username")
        password_raw = cf.get("discovery_password")
        community = cf.get("discovery_snmp_community")
        has_ssh = bool(username and password_raw)
        has_snmp = bool(community)

        if not has_ssh and not has_snmp:
            skipped_no_cred += 1
            continue

        needs_ssh = has_ssh and (args.force or not cf.get("discovery_ssh_status"))
        needs_snmp = has_snmp and (args.force or not cf.get("discovery_snmp_status"))
        if not needs_ssh and not needs_snmp:
            skipped_already_tested += 1
            continue

        print(f"[{'seria testado' if args.dry_run else 'testando'}] {device.name}", end="")
        ssh_status = ssh_detail = None
        snmp_status = snmp_detail = None

        if needs_ssh and not args.dry_run:
            password = core.decrypt_secret(password_raw)
            ok, detail = core.test_ssh_connectivity(device, username, password, cf.get("discovery_ssh_port"))
            ssh_status, ssh_detail = ("ok" if ok else "error"), detail
            print(f" SSH={'ok' if ok else 'FALHA (' + str(detail) + ')'}", end="")
        elif needs_ssh:
            print(" SSH=(dry-run)", end="")

        if needs_snmp and not args.dry_run:
            ok, detail = core.test_snmp_connectivity(device, community)
            snmp_status, snmp_detail = ("ok" if ok else "error"), detail
            print(f" SNMP={'ok' if ok else 'FALHA (' + str(detail) + ')'}", end="")
        elif needs_snmp:
            print(" SNMP=(dry-run)", end="")

        print()
        if not args.dry_run and (ssh_status is not None or snmp_status is not None):
            core.set_connectivity_status(
                device,
                ssh_status=ssh_status, ssh_status_detail=ssh_detail,
                snmp_status=snmp_status, snmp_status_detail=snmp_detail,
            )
        tested += 1

    print()
    print(f"Total de devices: {len(devices)}")
    print(f"  sem credencial nenhuma: {skipped_no_cred}")
    print(f"  já testados antes (pulados): {skipped_already_tested}")
    print(f"  {'seriam testados agora' if args.dry_run else 'testados agora'}: {tested}")
    if args.dry_run and tested:
        print("\nRode sem --dry-run pra aplicar de verdade.")


if __name__ == "__main__":
    main()
