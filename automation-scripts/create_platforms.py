#!/usr/bin/env python3
"""
create_platforms.py
====================
Cria (uma vez, por instalação) um conjunto padrão de Platforms no NetBox
-- usado como OVERRIDE opcional pro método de descoberta SSH (Netmiko),
que resolve o device_type automaticamente pelo Fabricante do device (ver
discovery_core.resolve_ssh_device_type()) mas também aceita
`Platform.slug` como valor manual quando o operador quer forçar um
device_type específico (ver discovery_core.py: `device_type =
device.platform.slug if device.platform else None`).

Cada slug abaixo é escolhido pra bater exatamente com o device_type do
Netmiko correspondente -- não troque o slug depois de criado, ou o
override para de funcionar.

Idempotente: pode rodar de novo sem duplicar (verifica se cada Platform
já existe, pelo slug, antes de criar).

Uso:
    python create_platforms.py

Variáveis de ambiente: NETBOX_URL, NETBOX_TOKEN (ou .env)
"""

import os
import sys

import pynetbox
from dotenv import load_dotenv

load_dotenv()

NETBOX_URL = os.environ.get("NETBOX_URL")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")

# slug -> nome de exibição. O slug É o device_type usado pelo Netmiko
# (discovery_core.py) -- ver lista completa em
# https://github.com/ktbyers/netmiko/blob/develop/PLATFORMS.md. Cobre os
# fabricantes reconhecidos automaticamente pelo Fabricante do device (ver
# _MANUFACTURER_SSH_RULES em discovery_core.py); huawei está aqui porque
# o catálogo de device types deste projeto é majoritariamente Huawei
# (NE8000 etc.).
PLATFORMS = {
    "huawei_vrp": "Huawei VRP",
    "cisco_ios": "Cisco IOS / IOS-XE",
    "cisco_xr": "Cisco IOS-XR",
    "cisco_nxos": "Cisco NX-OS (SSH)",
    "juniper_junos": "Juniper Junos",
    "arista_eos": "Arista EOS",
    "paloalto_panos": "Palo Alto PAN-OS",
}


def get_client() -> pynetbox.api:
    if not NETBOX_URL or not NETBOX_TOKEN:
        sys.exit("Defina NETBOX_URL e NETBOX_TOKEN (variáveis de ambiente ou .env).")
    return pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)


def main():
    nb = get_client()

    for slug, name in PLATFORMS.items():
        existing = nb.dcim.platforms.get(slug=slug)
        if existing:
            print(f"[já existe] {name} ({slug})")
            continue

        # napalm_driver NÃO é setado de propósito -- é um campo nativo do
        # NetBox pra integração dele com o NAPALM, que este projeto não
        # usa mais (ver decisão de padronizar em SSH puro via Netmiko).
        nb.dcim.platforms.create({
            "name": name,
            "slug": slug,
        })
        print(f"[criado] {name} ({slug})")

    print(
        "\nPronto. As Platforms aparecem em Devices > Platforms, e no "
        "dropdown 'Platform' do NetBox Oracle -- só precisa disso se "
        "quiser FORÇAR manualmente o device_type usado na descoberta SSH "
        "(o normal é o Fabricante resolver isso sozinho). Se o fabricante "
        "do device não estiver na lista, crie manualmente (Slug precisa "
        "bater com um device_type válido do Netmiko: "
        "https://github.com/ktbyers/netmiko/blob/develop/PLATFORMS.md)."
    )


if __name__ == "__main__":
    main()
