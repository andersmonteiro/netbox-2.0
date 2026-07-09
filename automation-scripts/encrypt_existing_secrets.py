#!/usr/bin/env python3
"""
encrypt_existing_secrets.py
============================
Migração única: cifra o custom field discovery_password de todo device
que ainda estiver com ele em TEXTO PURO (salvo antes da introdução de
encrypt_secret()/decrypt_secret() em discovery_core.py).

Sem rodar isso, uma senha só é cifrada quando o operador salva ela de
novo pela tela (dashboard ou /device/<id>/edit) -- qualquer senha já
cadastrada antes continua em texto puro no NetBox indefinidamente. Este
script varre TODOS os devices de uma vez e resolve isso sem precisar
reabrir/resalvar cada um manualmente.

Idempotente: um valor que já está cifrado (prefixo "enc:v1:", ver
discovery_core._SECRET_PREFIX) é ignorado -- pode rodar mais de uma vez
sem risco de cifrar em cima de cifrado.

Uso:
    python encrypt_existing_secrets.py            # varre e aplica
    python encrypt_existing_secrets.py --dry-run  # só mostra quem seria alterado

Usa a mesma URL/token que a interface web usa de verdade -- prioriza
discovery_output/_oracle/settings.json (config trocada pela tela de
Configurações) e só cai pra NETBOX_URL/NETBOX_TOKEN do .env se esse
arquivo não existir (ver _load_netbox_url_token() abaixo; mesma lógica
de discovery-ui/app.py:load_settings()). Também precisa da MESMA chave
usada pela interface web pra cifrar (FLASK_SECRET_KEY ou
DISCOVERY_UI_SECRET_KEY -- ver .env), senão o valor gravado por este
script não seria decifrável pela app depois. Rode isso dentro do
container netbox-oracle (mesmo ambiente/.env e mesmo discovery_output/
que a interface web usa):

    docker compose exec netbox-oracle python encrypt_existing_secrets.py --dry-run
"""

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

import discovery_core as core

load_dotenv()

# Mesma lógica de discovery-ui/app.py (load_settings()) -- a URL/token
# "de verdade" pode estar salva em discovery_output/_oracle/settings.json
# em vez de NETBOX_URL/NETBOX_TOKEN do .env, por causa do bug conhecido
# netbox-community/netbox-docker#1647 (o token gerado na instalação às
# vezes não bate com o do .env, então a tela de Configurações permite
# trocar sem precisar editar .env/reiniciar container). Sem isso, esse
# script batia direto no NETBOX_TOKEN do .env e tomava 403 "Invalid v1
# token" mesmo com a interface web funcionando normalmente (ela lê daqui,
# não só do .env).
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
        description="Cifra discovery_password de devices que ainda estão com a senha em texto puro no NetBox."
    )
    parser.add_argument("--dry-run", action="store_true", help="Só lista quem seria alterado, não grava nada")
    args = parser.parse_args()

    if core.Fernet is None:
        print(
            "[erro] Pacote 'cryptography' não está instalado nesse ambiente -- "
            "instale com 'pip install cryptography' (ou rode dentro do container "
            "netbox-oracle, que já tem) antes de continuar."
        )
        return

    if core._fernet() is None:
        print(
            "[erro] Não encontrei FLASK_SECRET_KEY nem DISCOVERY_UI_SECRET_KEY no "
            "ambiente/.env -- sem essa chave não dá pra cifrar de forma que a "
            "interface web consiga decifrar depois. Rode isso no mesmo ambiente "
            "(.env) do container netbox-oracle."
        )
        return

    url, token = _load_netbox_url_token()
    if not url or not token:
        print(
            "[erro] NetBox não configurado -- sem netbox_url/netbox_token em "
            f"{_SETTINGS_PATH} nem NETBOX_URL/NETBOX_TOKEN no ambiente."
        )
        return
    nb = core.get_client(url, token)
    devices = list(nb.dcim.devices.all())

    already_encrypted, updated, skipped_empty = 0, 0, 0
    for device in devices:
        cf = device.custom_fields or {}
        raw = cf.get("discovery_password")
        if not raw:
            skipped_empty += 1
            continue
        if isinstance(raw, str) and raw.startswith(core._SECRET_PREFIX):
            already_encrypted += 1
            continue

        new_value = core.encrypt_secret(raw)
        print(f"[{'seria cifrado' if args.dry_run else 'cifrado'}] {device.name}")
        if not args.dry_run:
            new_cf = dict(cf)
            new_cf["discovery_password"] = new_value
            device.update({"custom_fields": new_cf})
        updated += 1

    print()
    print(f"Total de devices: {len(devices)}")
    print(f"  já cifrados antes: {already_encrypted}")
    print(f"  sem senha configurada: {skipped_empty}")
    print(f"  {'seriam cifrados agora' if args.dry_run else 'cifrados agora'}: {updated}")
    if args.dry_run and updated:
        print("\nRode sem --dry-run pra aplicar de verdade.")


if __name__ == "__main__":
    main()
