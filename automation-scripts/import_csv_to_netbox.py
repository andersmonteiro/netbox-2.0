#!/usr/bin/env python3
"""
import_csv_to_netbox.py
========================
Preenche o NetBox em massa a partir de planilhas (CSV ou XLSX), evitando
digitação manual repetitiva. Faz "upsert": se o objeto já existe (mesmo
nome/slug), atualiza; senão, cria.

Uso:
    python import_csv_to_netbox.py sites   planilhas/sites.xlsx
    python import_csv_to_netbox.py devices planilhas/devices.csv
    python import_csv_to_netbox.py ips     planilhas/ip_addresses.csv

Variáveis de ambiente esperadas (ou defina em um .env na mesma pasta):
    NETBOX_URL   -> ex: http://localhost:8000
    NETBOX_TOKEN -> token de API com permissão de escrita

Formato esperado das planilhas (colunas obrigatórias em negrito no README):

sites.xlsx/csv:
    name, slug, status, region, description

devices.csv:
    name, device_role, device_type, manufacturer, site, status, serial

ip_addresses.csv:
    address (ex: 10.0.0.1/24), status, device, interface, description

Ajuste os mapeamentos de coluna abaixo se sua planilha usar nomes diferentes.
"""

import os
import sys
import argparse

import pandas as pd
import pynetbox
from dotenv import load_dotenv

load_dotenv()

NETBOX_URL = os.environ.get("NETBOX_URL")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")


def get_client() -> pynetbox.api:
    if not NETBOX_URL or not NETBOX_TOKEN:
        sys.exit("Defina NETBOX_URL e NETBOX_TOKEN (variáveis de ambiente ou .env).")
    nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
    return nb


def read_table(path: str) -> pd.DataFrame:
    if path.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(path)
    else:
        df = pd.read_csv(path)
    # Normaliza nomes de coluna: minúsculo, sem espaços nas pontas
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df.where(pd.notnull(df), None)


def import_sites(nb: pynetbox.api, df: pd.DataFrame) -> None:
    for _, row in df.iterrows():
        payload = {
            "name": row["name"],
            "slug": row.get("slug") or row["name"].lower().replace(" ", "-"),
            "status": row.get("status", "active"),
            "description": row.get("description") or "",
        }
        if row.get("region"):
            region = nb.dcim.regions.get(name=row["region"])
            if not region:
                region = nb.dcim.regions.create(
                    name=row["region"], slug=row["region"].lower().replace(" ", "-")
                )
            payload["region"] = region.id

        existing = nb.dcim.sites.get(slug=payload["slug"])
        if existing:
            existing.update(payload)
            print(f"[atualizado] site: {payload['name']}")
        else:
            nb.dcim.sites.create(payload)
            print(f"[criado]     site: {payload['name']}")


def _get_or_create(endpoint, name, extra=None):
    obj = endpoint.get(name=name)
    if obj:
        return obj
    data = {"name": name, "slug": name.lower().replace(" ", "-")}
    if extra:
        data.update(extra)
    return endpoint.create(data)


def import_devices(nb: pynetbox.api, df: pd.DataFrame) -> None:
    for _, row in df.iterrows():
        site = nb.dcim.sites.get(name=row["site"]) or nb.dcim.sites.get(slug=row["site"])
        if not site:
            print(f"[erro] site '{row['site']}' não encontrado, pulando {row['name']}")
            continue

        manufacturer = _get_or_create(nb.dcim.manufacturers, row["manufacturer"])
        device_type = nb.dcim.device_types.get(
            model=row["device_type"], manufacturer_id=manufacturer.id
        )
        if not device_type:
            device_type = nb.dcim.device_types.create(
                {
                    "model": row["device_type"],
                    "slug": row["device_type"].lower().replace(" ", "-"),
                    "manufacturer": manufacturer.id,
                }
            )
        role = _get_or_create(nb.dcim.device_roles, row["device_role"], {"color": "9e9e9e"})

        payload = {
            "name": row["name"],
            "device_type": device_type.id,
            "role": role.id,
            "site": site.id,
            "status": row.get("status", "active"),
        }
        if row.get("serial"):
            payload["serial"] = str(row["serial"])

        existing = nb.dcim.devices.get(name=row["name"], site_id=site.id)
        if existing:
            existing.update(payload)
            print(f"[atualizado] device: {row['name']}")
        else:
            nb.dcim.devices.create(payload)
            print(f"[criado]     device: {row['name']}")


def import_ips(nb: pynetbox.api, df: pd.DataFrame) -> None:
    for _, row in df.iterrows():
        payload = {
            "address": row["address"],
            "status": row.get("status", "active"),
            "description": row.get("description") or "",
        }

        device = None
        interface = None
        if row.get("device") and row.get("interface"):
            device = nb.dcim.devices.get(name=row["device"])
            if device:
                interface = nb.dcim.interfaces.get(device_id=device.id, name=row["interface"])
                if interface:
                    payload["assigned_object_type"] = "dcim.interface"
                    payload["assigned_object_id"] = interface.id

        existing = nb.ipam.ip_addresses.get(address=row["address"])
        if existing:
            existing.update(payload)
            print(f"[atualizado] IP: {row['address']}")
        else:
            new_ip = nb.ipam.ip_addresses.create(payload)
            print(f"[criado]     IP: {row['address']}")

        # Define como IP primário do device, se aplicável
        if device and interface and row.get("primary", False):
            device.primary_ip4 = existing.id if existing else new_ip.id
            device.save()


IMPORTERS = {
    "sites": import_sites,
    "devices": import_devices,
    "ips": import_ips,
}


def main():
    parser = argparse.ArgumentParser(description="Importa planilhas para o NetBox.")
    parser.add_argument("tipo", choices=IMPORTERS.keys())
    parser.add_argument("arquivo", help="Caminho do CSV ou XLSX")
    args = parser.parse_args()

    nb = get_client()
    df = read_table(args.arquivo)
    IMPORTERS[args.tipo](nb, df)


if __name__ == "__main__":
    main()
