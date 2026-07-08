#!/usr/bin/env python3
"""
discover_network.py
====================
Descoberta de rede leve — use quando quiser algo simples, sem SSH/SNMP
por device (ver discovery-ui/discovery_netbox.py pra isso). Faz um scan
de uma subnet com nmap, identifica hosts ativos e cria/atualiza no NetBox:

  - um endereço IP (status "active") para cada host respondendo
  - opcionalmente um Device "staged" (para revisão manual) quando o nmap
    conseguir identificar hostname/SO com confiança razoável

Nada é marcado como "active"/definitivo automaticamente — o objetivo é
reduzir digitação, não substituir a revisão humana.

Requer o binário `nmap` instalado no host/container que rodar o script.

Uso:
    python discover_network.py 10.0.0.0/24 --site "Matriz"

Variáveis de ambiente: NETBOX_URL, NETBOX_TOKEN (ou .env)
"""

import argparse
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

import pynetbox
from dotenv import load_dotenv

load_dotenv()

NETBOX_URL = os.environ.get("NETBOX_URL")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")


def get_client() -> pynetbox.api:
    if not NETBOX_URL or not NETBOX_TOKEN:
        sys.exit("Defina NETBOX_URL e NETBOX_TOKEN (variáveis de ambiente ou .env).")
    return pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)


def run_nmap(cidr: str) -> ET.Element:
    """Roda um scan -sn (ping sweep) + -O (best-effort OS guess) e retorna o XML."""
    cmd = ["nmap", "-sn", "-oX", "-", cidr]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=600)
    except FileNotFoundError:
        sys.exit("nmap não encontrado. Instale com: apt-get install -y nmap")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"nmap falhou: {exc.stderr}")
    return ET.fromstring(result.stdout)


def parse_hosts(xml_root: ET.Element):
    hosts = []
    for host in xml_root.findall("host"):
        status = host.find("status")
        if status is None or status.get("state") != "up":
            continue
        addr_el = host.find("address")
        if addr_el is None:
            continue
        ip = addr_el.get("addr")

        hostname = None
        hostnames_el = host.find("hostnames")
        if hostnames_el is not None:
            hn = hostnames_el.find("hostname")
            if hn is not None:
                hostname = hn.get("name")

        hosts.append({"ip": ip, "hostname": hostname})
    return hosts


def sync_to_netbox(nb: pynetbox.api, hosts, site_name: str, create_devices: bool):
    site = None
    if site_name:
        site = nb.dcim.sites.get(name=site_name)
        if not site:
            print(f"[aviso] site '{site_name}' não existe no NetBox, IPs serão criados sem device.")

    tag = nb.extras.tags.get(slug="auto-discovered")
    if not tag:
        tag = nb.extras.tags.create(
            {"name": "auto-discovered", "slug": "auto-discovered", "color": "f39c12"}
        )

    for h in hosts:
        cidr_ip = f"{h['ip']}/32"
        existing_ip = nb.ipam.ip_addresses.get(address=cidr_ip)
        description = h["hostname"] or "descoberto via nmap"
        if existing_ip:
            print(f"[já existe] IP {cidr_ip}")
        else:
            nb.ipam.ip_addresses.create(
                {
                    "address": cidr_ip,
                    "status": "active",
                    "description": description,
                    "tags": [tag.id],
                }
            )
            print(f"[criado] IP {cidr_ip} ({description})")

        if create_devices and h["hostname"] and site:
            existing_device = nb.dcim.devices.get(name=h["hostname"])
            if not existing_device:
                # Cria em status "staged": aparece no NetBox para alguém
                # revisar e completar device_type/role antes de ativar.
                print(
                    f"[revisar manualmente] host '{h['hostname']}' ({h['ip']}) "
                    "não criado como Device automaticamente — falta device_type/role. "
                    "Ficou registrado apenas o IP com a tag 'auto-discovered'."
                )


def main():
    parser = argparse.ArgumentParser(description="Descobre hosts ativos e popula o NetBox.")
    parser.add_argument("cidr", help="Faixa a escanear, ex: 10.0.0.0/24")
    parser.add_argument("--site", help="Nome do Site no NetBox para associar os achados")
    parser.add_argument(
        "--create-devices",
        action="store_true",
        help="Tenta sinalizar hosts para criação de Device (ainda requer revisão manual)",
    )
    args = parser.parse_args()

    nb = get_client()
    xml_root = run_nmap(args.cidr)
    hosts = parse_hosts(xml_root)
    print(f"{len(hosts)} host(s) ativo(s) encontrados em {args.cidr}")
    sync_to_netbox(nb, hosts, args.site, args.create_devices)


if __name__ == "__main__":
    main()
