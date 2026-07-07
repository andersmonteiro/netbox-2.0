#!/usr/bin/env python3
"""
discovery_netbox.py
====================
"Facilitador" de descoberta: lê Devices do NetBox que tiverem os custom
fields de descoberta preenchidos (ver create_discovery_fields.py),
coleta dados reais via SSH (NAPALM) ou SNMP (SNMPv2c), grava o
resultado em JSON para revisão humana, e só aplica no NetBox depois de
confirmação explícita.

Fluxo (dois comandos separados, de propósito -- nada é gravado no
NetBox sem passar pelo "apply"):

    python discovery_netbox.py collect [--site NOME] [--device NOME]
        -> descobre, grava um .json por device em discovery_output/,
           imprime um resumo na tela.

    (edite os .json manualmente se precisar corrigir algo antes)

    python discovery_netbox.py apply [--yes]
        -> lê os .json de discovery_output/, aplica no NetBox (cria
           interfaces que faltam, atualiza serial/enabled/descrição),
           move os arquivos aplicados para discovery_output/applied/.

Pré-requisito: rodar create_discovery_fields.py uma vez, e preencher em
cada Device os custom fields "discovery_method" (ssh/snmp) + a
credencial correspondente (Devices > editar o device > Custom Fields).

Pré-requisito SNMP: pacote `snmp` do sistema instalado (snmpget/
snmpwalk -- `apt-get install -y snmp`; já incluso no bootstrap.sh). Só
SNMPv2c por enquanto (sem suporte a v3 nesta primeira versão).

discovery_output/ e discovery_output/applied/ ficam com dado real de
cliente (IP, interface, etc.) -- por isso estão no .gitignore
(**/client-data/ cobre isso; confira antes de dar commit se mover a
pasta).

Variáveis de ambiente: NETBOX_URL, NETBOX_TOKEN (ou .env)
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pynetbox
from dotenv import load_dotenv

load_dotenv()

NETBOX_URL = os.environ.get("NETBOX_URL")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")

OUTPUT_DIR = Path(__file__).parent / "discovery_output"
APPLIED_DIR = OUTPUT_DIR / "applied"

# OIDs padrão da MIB-II (RFC 1213) -- suficiente pra hostname/descrição/
# interfaces sem depender de MIB proprietária de fabricante.
OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"  # ifXTable -- nome mais legível, quando o device suporta
OID_IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"


def get_client() -> pynetbox.api:
    if not NETBOX_URL or not NETBOX_TOKEN:
        sys.exit("Defina NETBOX_URL e NETBOX_TOKEN (variáveis de ambiente ou .env).")
    return pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)


# --------------------------------------------------------------------
# SNMP (via snmpget/snmpwalk do pacote `snmp` -- evita depender de lib
# Python de SNMP, que costuma dar dor de cabeça de versão/instalação;
# mesmo padrão do discover_network.py, que já shella pro `nmap`)
# --------------------------------------------------------------------

def _snmp_cmd(binary, host, community, oid, timeout, retries):
    return [
        binary, "-v2c", "-c", community,
        "-O", "qn",  # q = sem "Type:"/enum verboso na saída, n = OID numérico
        "-t", str(timeout), "-r", str(retries),
        host, oid,
    ]


def snmp_get(host, community, oid, timeout=5, retries=1):
    cmd = _snmp_cmd("snmpget", host, community, oid, timeout, retries)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout * (retries + 1) + 5)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "snmpget falhou (sem stderr)")
    line = result.stdout.strip()
    if not line:
        return None
    _, _, value = line.partition(" ")
    return value.strip().strip('"')


def snmp_walk(host, community, oid, timeout=5, retries=1):
    """Retorna dict {indice_da_tabela: valor} a partir de um walk."""
    cmd = _snmp_cmd("snmpwalk", host, community, oid, timeout, retries)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout * (retries + 1) + 15)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "snmpwalk falhou (sem stderr)")
    out = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        oid_full, _, value = line.partition(" ")
        idx = oid_full.rsplit(".", 1)[-1]  # último componente do OID = ifIndex
        out[idx] = value.strip().strip('"')
    return out


def _normalize_status(raw):
    """Aceita tanto '1'/'2' quanto 'up(1)'/'down(2)' (varia conforme o
    device e a versão do net-snmp instalada)."""
    if raw is None:
        return "unknown"
    raw = raw.strip().lower()
    if raw.startswith("1") or "up" in raw:
        return "up"
    if raw.startswith("2") or "down" in raw:
        return "down"
    return "unknown"


# --------------------------------------------------------------------
# Normalização de nome de interface -- necessário porque o Device Type
# no NetBox costuma já vir com um Interface Template (ex: NE8000 com
# 48 portas pré-cadastradas como "GigabitEthernet0/0/1"), mas o que
# volta da descoberta (SSH via NAPALM, ou SNMP via ifDescr/ifName) pode
# usar abreviação diferente (ex: "Gi0/0/1" ou "GE0/0/1"). Comparação de
# string exata faria o apply() achar que são portas diferentes e criar
# uma interface NOVA duplicada (tipo "other") em vez de reaproveitar a
# porta que o template já criou. Normalizamos os dois lados (nome já
# existente no NetBox E nome descoberto) pra um formato canônico antes
# de comparar -- best-effort, cobre as abreviações mais comuns
# (Cisco/Arista/Huawei-like); não cobre 100% dos vendors.
# --------------------------------------------------------------------
_IFACE_ABBREV = [
    ("tengigabitethernet", "tengigabitethernet"),
    ("tengige", "tengigabitethernet"),
    ("tengig", "tengigabitethernet"),
    ("te", "tengigabitethernet"),
    ("gigabitethernet", "gigabitethernet"),
    ("gige", "gigabitethernet"),
    ("gig", "gigabitethernet"),
    ("ge", "gigabitethernet"),
    ("gi", "gigabitethernet"),
    ("fastethernet", "fastethernet"),
    ("fa", "fastethernet"),
    ("ethernet", "ethernet"),
    ("eth", "ethernet"),
    ("et", "ethernet"),
    ("port-channel", "portchannel"),
    ("portchannel", "portchannel"),
    ("po", "portchannel"),
    ("loopback", "loopback"),
    ("lo", "loopback"),
    ("vlan", "vlan"),
    ("vl", "vlan"),
    ("management", "management"),
    ("mgmt", "management"),
    ("ma", "management"),
]
_IFACE_ABBREV_SORTED = sorted(_IFACE_ABBREV, key=lambda pair: -len(pair[0]))


def _normalize_ifname(name):
    """'Gi0/0/1' e 'GigabitEthernet0/0/1' -> mesma string normalizada."""
    if not name:
        return ""
    s = name.strip().lower()
    m = re.match(r"^([a-z\-]+)(.*)$", s)
    if not m:
        return re.sub(r"[^a-z0-9]", "", s)
    prefix, rest = m.group(1), m.group(2)
    rest_clean = re.sub(r"[^a-z0-9]", "", rest)
    for abbrev, full in _IFACE_ABBREV_SORTED:
        if prefix == abbrev:
            return f"{full}{rest_clean}"
    return f"{re.sub(r'[^a-z0-9]', '', prefix)}{rest_clean}"


def collect_snmp(device, community, timeout=5, retries=1):
    host = str(device.primary_ip4).split("/")[0] if device.primary_ip4 else None
    if not host:
        return {"method": "snmp", "errors": ["sem IP de gerência (primary_ip4)"]}

    data = {"method": "snmp", "host": host, "errors": []}
    try:
        data["sys_name"] = snmp_get(host, community, OID_SYS_NAME, timeout, retries)
        data["sys_descr"] = snmp_get(host, community, OID_SYS_DESCR, timeout, retries)

        if_descr = snmp_walk(host, community, OID_IF_DESCR, timeout, retries)
        if_name = snmp_walk(host, community, OID_IF_NAME, timeout, retries)
        admin_status = snmp_walk(host, community, OID_IF_ADMIN_STATUS, timeout, retries)
        oper_status = snmp_walk(host, community, OID_IF_OPER_STATUS, timeout, retries)

        interfaces = []
        for idx, descr in if_descr.items():
            interfaces.append(
                {
                    "index": idx,
                    "name": if_name.get(idx) or descr,
                    "descr": descr,
                    "admin_status": _normalize_status(admin_status.get(idx)),
                    "oper_status": _normalize_status(oper_status.get(idx)),
                }
            )
        data["interfaces"] = interfaces
    except Exception as exc:
        data["errors"].append(str(exc))
    return data


# --------------------------------------------------------------------
# SSH / NAPALM (mesma lógica do napalm_collect.py, mas com credencial
# por device em vez de usuário/senha único via CLI)
# --------------------------------------------------------------------

def collect_ssh(device, username, password):
    from napalm import get_network_driver  # import tardio: só quem usar SSH precisa do napalm

    if not device.platform:
        return {"method": "ssh", "errors": ["sem 'platform' definido (driver NAPALM)"]}
    if not device.primary_ip4:
        return {"method": "ssh", "errors": ["sem IP de gerência (primary_ip4)"]}

    host = str(device.primary_ip4).split("/")[0]
    driver_name = device.platform.slug
    data = {"method": "ssh", "host": host, "errors": []}

    try:
        driver = get_network_driver(driver_name)
    except Exception as exc:
        data["errors"].append(f"driver NAPALM '{driver_name}' inválido: {exc}")
        return data

    conn = driver(hostname=host, username=username, password=password)
    try:
        conn.open()
        facts = conn.get_facts()
        napalm_interfaces = conn.get_interfaces()
    except Exception as exc:
        data["errors"].append(f"falha ao conectar: {exc}")
        return data
    finally:
        try:
            conn.close()
        except Exception:
            pass

    data["serial"] = facts.get("serial_number") or None
    data["os_version"] = facts.get("os_version") or None
    data["interfaces"] = [
        {
            "name": name,
            "descr": info.get("description") or "",
            "admin_status": "up" if info.get("is_enabled") else "down",
            "oper_status": "up" if info.get("is_up") else "down",
            "mac_address": info.get("mac_address") or None,
        }
        for name, info in napalm_interfaces.items()
    ]
    return data


# --------------------------------------------------------------------
# collect
# --------------------------------------------------------------------

def cmd_collect(args):
    nb = get_client()
    filters = {}
    if args.site:
        filters["site"] = args.site
    if args.device:
        filters["name"] = args.device
    devices = list(nb.dcim.devices.filter(**filters))

    candidates = [d for d in devices if (d.custom_fields or {}).get("discovery_method")]
    if not candidates:
        print(
            "Nenhum device com 'discovery_method' preenchido encontrado. "
            "Rode create_discovery_fields.py (se ainda não rodou) e preencha "
            "o campo em Devices > (editar) > Custom Fields."
        )
        return

    OUTPUT_DIR.mkdir(exist_ok=True)
    print(f"{len(candidates)} device(s) marcado(s) para descoberta.\n")

    for device in candidates:
        cf = device.custom_fields or {}
        method = cf.get("discovery_method")
        print(f"--- {device.name} ({method}) ---")

        if method == "ssh":
            username = cf.get("discovery_username")
            password = cf.get("discovery_password")
            if not username or not password:
                print("  [pulado] discovery_method=ssh mas falta usuário/senha nos custom fields")
                continue
            result = collect_ssh(device, username, password)
        elif method == "snmp":
            community = cf.get("discovery_snmp_community")
            if not community:
                print("  [pulado] discovery_method=snmp mas falta a community nos custom fields")
                continue
            result = collect_snmp(device, community, timeout=args.snmp_timeout, retries=args.snmp_retries)
        else:
            print(f"  [pulado] discovery_method='{method}' desconhecido (use 'ssh' ou 'snmp')")
            continue

        result["device_id"] = device.id
        result["device_name"] = device.name
        result["collected_at"] = datetime.now(timezone.utc).isoformat()

        if result.get("errors"):
            for err in result["errors"]:
                print(f"  [erro] {err}")
            if not result.get("interfaces") and not result.get("sys_name"):
                print()
                continue  # nada útil coletado, não grava json

        out_path = OUTPUT_DIR / f"{device.name}.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        n_if = len(result.get("interfaces", []))
        up = sum(1 for i in result.get("interfaces", []) if i.get("oper_status") == "up")
        if result.get("sys_name"):
            print(f"  sys_name: {result['sys_name']}")
        print(f"  interfaces: {n_if} ({up} up)")
        print(f"  -> {out_path}")
        print()

    print(
        f"Arquivos em {OUTPUT_DIR}/ -- revise/edite se precisar, depois rode:\n"
        "  python discovery_netbox.py apply\n"
    )


# --------------------------------------------------------------------
# apply
# --------------------------------------------------------------------

def cmd_apply(args):
    if not OUTPUT_DIR.exists() or not any(OUTPUT_DIR.glob("*.json")):
        print(f"Nada pra aplicar em {OUTPUT_DIR}/ (rode 'collect' primeiro).")
        return

    nb = get_client()
    json_files = sorted(OUTPUT_DIR.glob("*.json"))

    print(f"{len(json_files)} arquivo(s) pendente(s):")
    for f in json_files:
        print(f"  - {f.name}")

    if not args.yes:
        confirm = input("\nAplicar essas mudanças no NetBox agora? (y/N): ").strip().lower()
        if confirm != "y":
            print("Cancelado -- nada foi alterado.")
            return

    APPLIED_DIR.mkdir(exist_ok=True)

    for f in json_files:
        data = json.loads(f.read_text(encoding="utf-8"))
        device_id = data.get("device_id")
        device_name = data.get("device_name", "?")
        device = nb.dcim.devices.get(device_id) if device_id else None
        if not device:
            print(f"[erro] {device_name}: device_id {device_id} não encontrado mais no NetBox, pulando")
            continue

        changes = []

        serial = data.get("serial")
        if serial and serial != device.serial:
            device.update({"serial": serial})
            changes.append(f"serial={serial}")

        existing_if = list(nb.dcim.interfaces.filter(device_id=device.id))
        existing_by_norm = {_normalize_ifname(i.name): i for i in existing_if}
        created, updated, matched_norm = 0, 0, 0
        for iface in data.get("interfaces", []):
            name = iface.get("name")
            if not name:
                continue
            enabled = iface.get("admin_status") == "up"
            description = iface.get("descr") or ""
            nb_if = existing_by_norm.get(_normalize_ifname(name))
            if nb_if:
                if nb_if.name != name:
                    matched_norm += 1
                    print(f"  [interface] '{name}' descoberto == '{nb_if.name}' já existente (nome normalizado)")
                if nb_if.enabled != enabled or (description and nb_if.description != description):
                    nb_if.update({"enabled": enabled, "description": description or nb_if.description})
                    updated += 1
            else:
                nb.dcim.interfaces.create(
                    {
                        "device": device.id,
                        "name": name,
                        "type": "other",
                        "enabled": enabled,
                        "description": description,
                        "mac_address": iface.get("mac_address") or None,
                    }
                )
                created += 1
        if created:
            changes.append(f"{created} interface(s) criada(s)")
        if updated:
            changes.append(f"{updated} interface(s) atualizada(s)")
        if matched_norm:
            changes.append(f"{matched_norm} casada(s) por nome normalizado")

        if changes:
            print(f"[aplicado] {device_name}: {', '.join(changes)}")
        else:
            print(f"[sem mudanças] {device_name}")

        f.rename(APPLIED_DIR / f.name)

    print(f"\nConcluído. Arquivos aplicados movidos para {APPLIED_DIR}/.")


def main():
    parser = argparse.ArgumentParser(
        description="Descobre devices (SSH/SNMP) a partir de credenciais cadastradas no NetBox, com revisão humana antes de aplicar."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect", help="Coleta dados e grava JSON para revisão (não altera o NetBox)")
    p_collect.add_argument("--site", help="Restringe aos devices de um Site")
    p_collect.add_argument("--device", help="Restringe a um device específico (nome exato)")
    p_collect.add_argument("--snmp-timeout", type=int, default=5, help="Timeout por operação SNMP, em segundos (default: 5)")
    p_collect.add_argument("--snmp-retries", type=int, default=1, help="Retentativas SNMP (default: 1)")
    p_collect.set_defaults(func=cmd_collect)

    p_apply = sub.add_parser("apply", help="Aplica os JSON de discovery_output/ no NetBox")
    p_apply.add_argument("--yes", action="store_true", help="Não pede confirmação interativa")
    p_apply.set_defaults(func=cmd_apply)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
