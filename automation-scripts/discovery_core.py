#!/usr/bin/env python3
"""
discovery_core.py
==================
Lógica compartilhada de descoberta (SSH/NAPALM e SNMP) e aplicação no
NetBox. Não tem interface própria -- é importado por:

  - discovery_netbox.py    (CLI: collect / apply via JSON em disco)
  - discovery-ui/app.py    (interface web: mesmo fluxo, mas com revisão
                             e edição na tela em vez de editar JSON)

Mantido separado do CLI de propósito, pra não duplicar a lógica de
coleta/aplicação entre os dois front-ends.
"""

import os
import re
import subprocess
import sys

import pynetbox

# --------------------------------------------------------------------
# Cliente NetBox
# --------------------------------------------------------------------

def get_client(url=None, token=None):
    """Se url/token não forem passados, cai pra NETBOX_URL/NETBOX_TOKEN
    do ambiente (uso do CLI). A interface web sempre passa explícito."""
    url = url or os.environ.get("NETBOX_URL")
    token = token or os.environ.get("NETBOX_TOKEN")
    if not url or not token:
        sys.exit("Defina NETBOX_URL e NETBOX_TOKEN (variáveis de ambiente ou .env).")
    return pynetbox.api(url, token=token)


# --------------------------------------------------------------------
# SNMP (via snmpget/snmpwalk do pacote `snmp` -- evita depender de lib
# Python de SNMP, que costuma dar dor de cabeça de versão/instalação;
# mesmo padrão do discover_network.py, que já shella pro `nmap`)
# --------------------------------------------------------------------

OID_SYS_NAME = "1.3.6.1.2.1.1.5.0"
OID_SYS_DESCR = "1.3.6.1.2.1.1.1.0"
OID_IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
OID_IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"  # ifXTable -- nome mais legível, quando o device suporta
OID_IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"


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
# SSH / NAPALM
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


def collect_device(device, method, credentials, snmp_timeout=5, snmp_retries=1):
    """Ponto de entrada único: despacha pro coletor certo a partir do
    'discovery_method' + dict de credenciais (mesmas chaves dos custom
    fields: username/password ou community)."""
    if method == "ssh":
        username = credentials.get("discovery_username")
        password = credentials.get("discovery_password")
        if not username or not password:
            return {"method": "ssh", "errors": ["falta usuário/senha"]}
        return collect_ssh(device, username, password)
    elif method == "snmp":
        community = credentials.get("discovery_snmp_community")
        if not community:
            return {"method": "snmp", "errors": ["falta a community"]}
        return collect_snmp(device, community, timeout=snmp_timeout, retries=snmp_retries)
    else:
        return {"method": method, "errors": [f"método desconhecido: '{method}' (use 'ssh' ou 'snmp')"]}


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


def normalize_ifname(name):
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


# --------------------------------------------------------------------
# apply -- grava o resultado de uma coleta (já revisado) no NetBox
# --------------------------------------------------------------------

def apply_device_result(nb, device, data, interface_filter=None):
    """Aplica um resultado de coleta (dict, mesmo formato salvo em JSON
    pelo CLI) num Device já carregado do pynetbox.

    interface_filter: dict opcional {nome_da_interface: {"include": bool,
    "description": str}} -- usado pela interface web pra permitir marcar
    quais interfaces entram e editar a descrição antes de aplicar. Se
    None, aplica todas as interfaces descobertas como vieram (comportamento
    do CLI).

    Retorna a lista de strings descrevendo o que mudou (changes).
    """
    changes = []

    serial = data.get("serial")
    if serial and serial != device.serial:
        device.update({"serial": serial})
        changes.append(f"serial={serial}")

    existing_if = list(nb.dcim.interfaces.filter(device_id=device.id))
    existing_by_norm = {normalize_ifname(i.name): i for i in existing_if}
    created, updated, matched_norm = 0, 0, 0

    for iface in data.get("interfaces", []):
        name = iface.get("name")
        if not name:
            continue

        if interface_filter is not None:
            entry = interface_filter.get(name)
            if not entry or not entry.get("include", True):
                continue
            description = entry.get("description", iface.get("descr") or "")
        else:
            description = iface.get("descr") or ""

        enabled = iface.get("admin_status") == "up"
        nb_if = existing_by_norm.get(normalize_ifname(name))
        if nb_if:
            if nb_if.name != name:
                matched_norm += 1
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

    return changes


# --------------------------------------------------------------------
# Helpers usados só pela interface web (discovery-ui/) -- cadastro de
# device novo e IP de gerência sem precisar entrar no NetBox direto.
# --------------------------------------------------------------------

MGMT_INTERFACE_NAME = "mgmt-discovery"


def set_primary_ip(nb, device, ip_str):
    """Garante que 'device' tenha 'ip_str' (ex: '10.0.0.1' ou
    '10.0.0.1/32') como Primary IPv4. No modelo do NetBox, um IP só
    pode virar Primary IPv4 se já estiver associado a uma Interface do
    device -- por isso criamos (ou reaproveitamos) uma interface
    dedicada MGMT_INTERFACE_NAME pra isso, em vez de exigir que o
    operador entenda esse detalhe do modelo de dados.
    """
    ip_str = ip_str.strip()
    if "/" not in ip_str:
        ip_str = f"{ip_str}/32"

    iface = nb.dcim.interfaces.get(device_id=device.id, name=MGMT_INTERFACE_NAME)
    if not iface:
        iface = nb.dcim.interfaces.create(
            {"device": device.id, "name": MGMT_INTERFACE_NAME, "type": "virtual"}
        )

    ip_obj = nb.ipam.ip_addresses.get(address=ip_str, device_id=device.id)
    if not ip_obj:
        # Pode já existir um IP com esse endereço em outro objeto (ex:
        # criado antes sem interface associada) -- tenta achar por
        # endereço puro antes de criar um novo, pra não duplicar.
        ip_obj = nb.ipam.ip_addresses.get(address=ip_str)
    if not ip_obj:
        ip_obj = nb.ipam.ip_addresses.create(
            {"address": ip_str, "assigned_object_type": "dcim.interface", "assigned_object_id": iface.id}
        )
    elif not ip_obj.assigned_object_id:
        ip_obj.update({"assigned_object_type": "dcim.interface", "assigned_object_id": iface.id})

    device.update({"primary_ip4": ip_obj.id})
    return ip_obj


def set_discovery_fields(device, method, discovery_username=None, discovery_password=None, discovery_snmp_community=None):
    """Grava discovery_method + a credencial correspondente nos custom
    fields do device (mesmos campos criados por create_discovery_fields.py)."""
    cf = dict(device.custom_fields or {})
    cf["discovery_method"] = method
    if method == "ssh":
        if discovery_username is not None:
            cf["discovery_username"] = discovery_username
        if discovery_password is not None:
            cf["discovery_password"] = discovery_password
    elif method == "snmp":
        if discovery_snmp_community is not None:
            cf["discovery_snmp_community"] = discovery_snmp_community
    device.update({"custom_fields": cf})
    return device


def create_device(nb, name, site_id, role_id, device_type_id, status="active"):
    return nb.dcim.devices.create(
        {
            "name": name,
            "site": site_id,
            "role": role_id,
            "device_type": device_type_id,
            "status": status,
        }
    )
