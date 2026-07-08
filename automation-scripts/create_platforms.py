#!/usr/bin/env python3
"""
create_platforms.py
====================
Cria (uma vez, por instalação) um conjunto padrão de Platforms no NetBox
-- necessário pro método de descoberta SSH (NAPALM), que usa o campo
`Platform.slug` do device pra saber qual driver NAPALM usar (ver
discovery_core.py: `driver_name = device.platform.slug`). Sem uma
Platform cadastrada com o slug certo, o dropdown de Platform no NetBox
Oracle fica vazio e a descoberta via SSH não roda.

Cada slug abaixo é escolhido pra bater exatamente com o nome do driver
NAPALM correspondente -- não troque o slug depois de criado, ou a
descoberta SSH para de achar o driver.

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

# slug -> (nome de exibição, napalm_driver). O slug É o driver usado pelo
# discovery_core.py -- os drivers "nativos" do napalm core são eos, ios,
# iosxr, junos, nxos, nxos_ssh e panos; huawei_vrp é um driver
# comunitário (pacote napalm-huawei-vrp, ver requirements.txt) incluído
# aqui porque o catálogo de device types deste projeto é majoritariamente
# Huawei (NE8000 etc.).
PLATFORMS = {
    "huawei_vrp": "Huawei VRP",
    "ios": "Cisco IOS / IOS-XE",
    "iosxr": "Cisco IOS-XR",
    "nxos_ssh": "Cisco NX-OS (SSH)",
    "junos": "Juniper Junos",
    "eos": "Arista EOS",
    "panos": "Palo Alto PAN-OS",
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

        nb.dcim.platforms.create({
            "name": name,
            "slug": slug,
            "napalm_driver": slug,
        })
        print(f"[criado] {name} ({slug})")

    print(
        "\nPronto. As Platforms aparecem em Devices > Platforms, e no "
        "dropdown 'Platform' do NetBox Oracle. Se o fabricante do device "
        "não estiver na lista, crie manualmente (Slug precisa bater com "
        "o nome do driver NAPALM: https://napalm.readthedocs.io/en/latest/support/)."
    )


if __name__ == "__main__":
    main()
