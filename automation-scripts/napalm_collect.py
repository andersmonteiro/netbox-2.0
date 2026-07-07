#!/usr/bin/env python3
"""
napalm_collect.py
==================
Conecta em devices já cadastrados no NetBox (via SSH/API, usando NAPALM) e
preenche automaticamente: número de série, versão de SO e a lista de
interfaces (criando as que faltam). Útil para devices que já existem no
NetBox mas foram cadastrados "no capricho" e estão incompletos.

Como o NetBox sabe em quais devices conectar: qualquer Device com o campo
"platform" preenchido com um driver NAPALM válido (ios, eos, junos,
nxos_ssh, iosxr, etc.) e com um IP de gerência (primary_ip4) definido.

Uso:
    python napalm_collect.py --username admin --password 'senha' \
        --secret 'enable_secret_opcional'

    # Ou restrinja a um site específico:
    python napalm_collect.py --site "Matriz" --username admin --password senha

Variáveis de ambiente: NETBOX_URL, NETBOX_TOKEN (ou .env)
"""

import argparse
import os
import sys

import pynetbox
from dotenv import load_dotenv
from napalm import get_network_driver

load_dotenv()

NETBOX_URL = os.environ.get("NETBOX_URL")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")


def get_client() -> pynetbox.api:
    if not NETBOX_URL or not NETBOX_TOKEN:
        sys.exit("Defina NETBOX_URL e NETBOX_TOKEN (variáveis de ambiente ou .env).")
    return pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)


def collect_device(nb, device, username, password, secret):
    if not device.platform:
        print(f"[pulado] {device.name}: sem 'platform' definido")
        return
    if not device.primary_ip4:
        print(f"[pulado] {device.name}: sem IP de gerência (primary_ip4)")
        return

    driver_name = device.platform.slug
    host = str(device.primary_ip4).split("/")[0]

    try:
        driver = get_network_driver(driver_name)
    except Exception:
        print(f"[erro] {device.name}: driver NAPALM '{driver_name}' inválido")
        return

    optional_args = {"secret": secret} if secret else {}
    conn = driver(hostname=host, username=username, password=password, optional_args=optional_args)

    try:
        conn.open()
        facts = conn.get_facts()
        interfaces = conn.get_interfaces()
    except Exception as exc:
        print(f"[erro] {device.name} ({host}): falha ao conectar - {exc}")
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Atualiza serial/descrição do device
    updates = {}
    if facts.get("serial_number") and facts["serial_number"] != device.serial:
        updates["serial"] = facts["serial_number"]
    if updates:
        device.update(updates)
        print(f"[atualizado] {device.name}: {updates}")

    # Cria interfaces que ainda não existem no NetBox
    existing = {i.name for i in nb.dcim.interfaces.filter(device_id=device.id)}
    created = 0
    for if_name, if_data in interfaces.items():
        if if_name in existing:
            continue
        nb.dcim.interfaces.create(
            {
                "device": device.id,
                "name": if_name,
                "type": "other",
                "enabled": if_data.get("is_enabled", True),
                "mac_address": if_data.get("mac_address") or None,
                "description": if_data.get("description") or "",
            }
        )
        created += 1
    if created:
        print(f"[atualizado] {device.name}: {created} interface(s) criada(s)")

    if not updates and not created:
        print(f"[sem mudanças] {device.name}")


def main():
    parser = argparse.ArgumentParser(description="Coleta dados reais dos devices via NAPALM.")
    parser.add_argument("--site", help="Restringe aos devices de um Site específico")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--secret", help="Enable secret (Cisco IOS, opcional)")
    args = parser.parse_args()

    nb = get_client()
    filters = {}
    if args.site:
        filters["site"] = args.site
    devices = nb.dcim.devices.filter(**filters)

    for device in devices:
        collect_device(nb, device, args.username, args.password, args.secret)


if __name__ == "__main__":
    main()
