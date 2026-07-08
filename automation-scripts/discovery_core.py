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


def collect_snmp(device, community, timeout=5, retries=1, ssh_username=None, ssh_password=None, ssh_port=None):
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

    # Extra opcional, só pra MikroTik: SNMP de leitura simples não expõe o
    # vínculo VLAN -> porta física no RouterOS (confirmado -- nem o
    # LibreNMS consegue isso sem instalar um script no device com SNMP
    # write habilitado, o que é um risco de segurança que este projeto
    # evita de propósito). Se o operador também informou uma credencial
    # SSH pra esse device (mesmos campos usados no método "ssh", só que
    # aqui como um extra opcional), tentamos ler
    # "/interface vlan print detail" via CLI pra confirmar o vínculo real.
    # Best-effort: qualquer falha aqui (sem netmiko, sem credencial, não é
    # MikroTik, timeout...) não derruba a coleta SNMP principal, que já
    # está pronta -- só fica sem essa dica extra, e o operador escolhe
    # manualmente na revisão.
    sys_descr = (data.get("sys_descr") or "").lower()
    looks_like_mikrotik = "routeros" in sys_descr or "mikrotik" in sys_descr
    if looks_like_mikrotik and ssh_username and ssh_password:
        try:
            details = collect_mikrotik_interface_details(host, ssh_username, ssh_password, port=ssh_port)
            for iface in data["interfaces"]:
                info = details.get(iface["name"])
                if not info:
                    continue
                # O comment= do RouterOS é o campo que o operador realmente
                # preenche no Winbox (ex: "SERVER-ANM2000") -- bem diferente
                # do que o SNMP ifDescr retorna pra MikroTik, que é só o
                # nome cru da porta repetido. Sobrescreve descr só quando
                # tem comment de verdade (senão mantém o valor do SNMP).
                if info.get("comment"):
                    iface["descr"] = info["comment"]
                if info.get("parent"):
                    iface["vlan_parent_from_device"] = info["parent"]
        except Exception:
            pass  # best-effort -- ver comentário acima

    return data


def _ros_split_entries(output):
    """Cada item do "print terse" começa com um índice numérico no início
    da linha (ex: " 0  R  comment=... name=..."). Normalmente é uma linha
    só por item, mas separamos assim mesmo (em vez de linha por linha) pra
    ficar resiliente a uma eventual quebra de linha do terminal no meio de
    um item."""
    return re.split(r"\n(?=\s*\d+\s)", output)


def _ros_terse_value(text, key):
    """Extrai o valor de um campo 'key=valor' de uma saída 'print terse'
    do RouterOS -- confirmado contra device real (RouterOS 6.48.6). Nesse
    formato os valores NÃO vêm entre aspas, então quando o valor tem
    espaço (comentário livre do operador, ex: "SWITCH IBOPE", "CCR1072_
    CGNAT - PORTA DE TESTES") o RouterOS não delimita de jeito nenhum --
    a única forma de saber onde o valor termina é olhar onde começa o
    próximo campo "algumacoisa=" reconhecível (ou o fim do bloco). O
    lookbehind negativo evita casar por engano com um campo cujo nome
    termina com "key" (ex: procurar "name=" não pode casar dentro de
    "default-name=")."""
    m = re.search(rf"(?<![\w-]){key}=(.*?)(?:\s+[a-zA-Z][\w-]*=|\s*$)", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def collect_mikrotik_interface_details(host, username, password, port=None):
    """Conecta via SSH (Netmiko) num MikroTik/RouterOS e lê duas saídas em
    formato 'terse' (uma linha por interface -- mais simples e confiável
    de parsear que 'print detail', que varia o formato do comentário
    entre versões do RouterOS):

    - '/interface print terse': confirmado contra device real -- traz
      comment=<comentário livre do operador> (ex: "SERVER-ANM2000",
      "SWITCH IBOPE" -- é o que aparece no Winbox acima de cada porta,
      bem diferente do que o SNMP ifDescr retorna pra MikroTik) e
      name=<nome>. Quando a interface não tem comentário definido, o
      campo simplesmente não aparece na linha.
    - '/interface vlan print terse': o "print terse" genérico acima NÃO
      traz o vínculo VLAN -> porta física (campo "interface=") pras
      entradas tipo vlan -- esse campo só aparece nessa consulta
      específica de VLAN.

    Retorna {nome_da_interface: {"comment": str|None, "parent": str|None}}.

    Levanta exceção se netmiko não estiver instalado ou a conexão falhar
    -- quem chama (collect_snmp) trata isso como best-effort e ignora
    silenciosamente.
    """
    from netmiko import ConnectHandler  # import tardio: só quem usar essa checagem extra precisa do netmiko

    conn = ConnectHandler(
        device_type="mikrotik_routeros",
        host=host,
        username=username,
        password=password,
        port=int(port) if port else 22,
        timeout=10,
    )
    try:
        output = conn.send_command("/interface print terse without-paging")
        vlan_output = conn.send_command("/interface vlan print terse without-paging")
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass

    details = {}
    for entry in _ros_split_entries(output):
        name = _ros_terse_value(entry, "name")
        if not name:
            continue
        details[name] = {
            "comment": _ros_terse_value(entry, "comment"),
            "parent": None,
        }

    for entry in _ros_split_entries(vlan_output):
        name = _ros_terse_value(entry, "name")
        if not name:
            continue
        parent = _ros_terse_value(entry, "interface")
        details.setdefault(name, {"comment": None, "parent": None})
        if parent:
            details[name]["parent"] = parent

    return details


# --------------------------------------------------------------------
# SSH / NAPALM
# --------------------------------------------------------------------

def collect_ssh(device, username, password, port=None):
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

    optional_args = {}
    if port:
        try:
            optional_args["port"] = int(port)
        except (TypeError, ValueError):
            pass

    conn = driver(hostname=host, username=username, password=password, optional_args=optional_args or None)
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
        port = credentials.get("discovery_ssh_port")
        if not username or not password:
            return {"method": "ssh", "errors": ["falta usuário/senha"]}
        return collect_ssh(device, username, password, port=port)
    elif method == "snmp":
        community = credentials.get("discovery_snmp_community")
        if not community:
            return {"method": "snmp", "errors": ["falta a community"]}
        return collect_snmp(
            device, community, timeout=snmp_timeout, retries=snmp_retries,
            # Credencial SSH extra e opcional (mesmos campos do método
            # "ssh") -- só usada pra confirmar o vínculo VLAN -> porta
            # física em devices MikroTik, ver collect_snmp().
            ssh_username=credentials.get("discovery_username") or None,
            ssh_password=credentials.get("discovery_password") or None,
            ssh_port=credentials.get("discovery_ssh_port") or None,
        )
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
# Tipo de interface (campo "type" do NetBox) -- a descoberta (SNMP ou
# SSH/NAPALM) não traz esse dado de forma confiável, então tentamos
# adivinhar a partir do NOME da interface (mesma ideia de
# normalize_ifname acima) e deixamos o operador confirmar/trocar na
# tela de revisão antes de gravar. Sem isso, toda interface nova
# entrava no NetBox como "Other", que é o catch-all genérico e não
# aparece com o ícone/rótulo certo (ex: "SFP+ (10GE)", "1000BASE-T
# (1GE)") como as interfaces cadastradas manualmente.
# Lista enxuta com os slugs mais comuns -- cobre a maioria dos
# vendors deste projeto (Cisco/Huawei/MikroTik/Juniper/Arista).
# Referência: NetBox InterfaceTypeChoices (dcim/choices.py).
# --------------------------------------------------------------------
INTERFACE_TYPE_CHOICES = [
    ("virtual", "Virtual"),
    ("bridge", "Bridge"),
    ("lag", "Link Aggregation Group (LAG)"),
    ("100base-tx", "100BASE-TX (10/100ME)"),
    ("1000base-t", "1000BASE-T (1GE)"),
    ("2.5gbase-t", "2.5GBASE-T (2.5GE)"),
    ("5gbase-t", "5GBASE-T (5GE)"),
    ("10gbase-t", "10GBASE-T (10GE)"),
    ("1000base-x-sfp", "SFP (1GE)"),
    ("10gbase-x-sfpp", "SFP+ (10GE)"),
    ("25gbase-x-sfp28", "SFP28 (25GE)"),
    ("40gbase-x-qsfpp", "QSFP+ (40GE)"),
    ("100gbase-x-qsfp28", "QSFP28 (100GE)"),
    ("other", "Other"),
]


def guess_interface_type(name):
    """Chute do tipo NetBox a partir do nome da interface descoberta.
    Best-effort -- sempre revisável na tela de revisão antes de gravar."""
    n = (name or "").strip().lower()
    if not n:
        return "other"
    if n.startswith(("vlan", "vl", "loopback", "lo", "eoip", "tunnel", "gre")):
        return "virtual"
    if n.startswith("bridge") or n == "br" or n.startswith("br-"):
        return "bridge"
    if n.startswith(("port-channel", "portchannel", "po", "lag", "bond")):
        return "lag"
    if "sfpplus" in n or "sfp-plus" in n or n.startswith(("te", "xe")) or "tengig" in n:
        return "10gbase-x-sfpp"
    if n.startswith("qsfp") or "40gb" in n or "100gb" in n:
        return "40gbase-x-qsfpp"
    if "sfp" in n:
        return "1000base-x-sfp"
    if n.startswith("fa") or "fastethernet" in n:
        return "100base-tx"
    if n.startswith(("ether", "gi", "ge", "eth")) or "gigabitethernet" in n:
        return "1000base-t"
    return "other"


def is_vlan_ifname(name):
    """True se o nome parece uma sub-interface lógica que faz sentido
    vincular a uma porta física na revisão -- cobre tanto o padrão MikroTik
    (vlanN, sem relação de nome com a porta física) quanto o padrão
    dotted-notation usado por Cisco/Juniper/Huawei (ex: 'Gi0/1.100',
    'ge-0/0/1.100', onde o sufixo depois do ponto é o ID da VLAN)."""
    n = (name or "").strip().lower()
    if n.startswith("vlan") or n.startswith("vl"):
        return True
    if "." in n and n.rsplit(".", 1)[-1].isdigit():
        return True
    return False


def guess_parent_name(name, all_names):
    """Tenta adivinhar a interface física/pai de uma sub-interface a partir
    do próprio NOME -- só funciona pro padrão dotted-notation (Cisco/
    Juniper/Huawei), onde tudo antes do último ponto é o nome da interface
    pai (ex: 'GigabitEthernet0/1.100' -> pai 'GigabitEthernet0/1'). Só
    retorna um palpite se essa interface pai realmente aparecer na mesma
    coleta (all_names); senão retorna None e o operador escolhe manualmente
    no dropdown. Não cobre o padrão MikroTik ('vlan3' não tem o nome da
    porta física embutido) -- aí não tem chute por nome possível."""
    if not name or "." not in name:
        return None
    candidate = name.rsplit(".", 1)[0].strip()
    if not candidate or candidate == name:
        return None
    if candidate in all_names:
        return candidate
    cand_norm = normalize_ifname(candidate)
    for other in all_names:
        if other != name and normalize_ifname(other) == cand_norm:
            return other
    return None


def apply_device_result(nb, device, data, interface_filter=None):
    """Aplica um resultado de coleta (dict, mesmo formato salvo em JSON
    pelo CLI) num Device já carregado do pynetbox.

    interface_filter: dict opcional {nome_da_interface: {"include": bool,
    "description": str, "type": str|None, "parent_name": str|None}} --
    usado pela interface web pra permitir marcar quais interfaces
    entram, editar a descrição, confirmar o tipo (ver
    guess_interface_type) e -- pra interfaces de VLAN -- vincular à
    porta física correspondente (campo "parent" do NetBox) antes de
    aplicar. Se None, aplica todas as interfaces descobertas como
    vieram, sem tipo nem vínculo (comportamento do CLI).

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
    processed_by_name = {}  # nome descoberto -> objeto pynetbox da interface (nova ou já existente)
    pending_parents = []  # [(nome_da_vlan, nome_da_interface_pai)]

    for iface in data.get("interfaces", []):
        name = iface.get("name")
        if not name:
            continue

        iface_type = None
        parent_name = None
        if interface_filter is not None:
            entry = interface_filter.get(name)
            if not entry or not entry.get("include", True):
                continue
            description = entry.get("description", iface.get("descr") or "")
            iface_type = entry.get("type") or None
            parent_name = entry.get("parent_name") or None
        else:
            description = iface.get("descr") or ""

        enabled = iface.get("admin_status") == "up"
        nb_if = existing_by_norm.get(normalize_ifname(name))
        if nb_if:
            if nb_if.name != name:
                matched_norm += 1
            update_payload = {}
            if nb_if.enabled != enabled:
                update_payload["enabled"] = enabled
            if description and nb_if.description != description:
                update_payload["description"] = description
            if iface_type:
                update_payload["type"] = iface_type
            if update_payload:
                nb_if.update(update_payload)
                updated += 1
        else:
            nb_if = nb.dcim.interfaces.create(
                {
                    "device": device.id,
                    "name": name,
                    "type": iface_type or "other",
                    "enabled": enabled,
                    "description": description,
                    "mac_address": iface.get("mac_address") or None,
                }
            )
            created += 1

        processed_by_name[name] = nb_if
        if parent_name:
            pending_parents.append((name, parent_name))

    # Segunda passada: só agora todas as interfaces do lote (novas e
    # já existentes) têm um objeto/ID resolvido, então dá pra vincular
    # a VLAN na porta física escolhida na revisão (campo "parent").
    linked = 0
    for vlan_name, parent_name in pending_parents:
        vlan_if = processed_by_name.get(vlan_name)
        parent_if = processed_by_name.get(parent_name) or existing_by_norm.get(normalize_ifname(parent_name))
        if vlan_if and parent_if and vlan_if.id != parent_if.id:
            vlan_if.update({"parent": parent_if.id})
            linked += 1

    if created:
        changes.append(f"{created} interface(s) criada(s)")
    if updated:
        changes.append(f"{updated} interface(s) atualizada(s)")
    if matched_norm:
        changes.append(f"{matched_norm} casada(s) por nome normalizado")
    if linked:
        changes.append(f"{linked} VLAN(s) vinculada(s) à interface física")

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


def set_discovery_fields(device, method, discovery_username=None, discovery_password=None, discovery_snmp_community=None, discovery_ssh_port=None):
    """Grava discovery_method + a credencial correspondente nos custom
    fields do device (mesmos campos criados por create_discovery_fields.py).

    username/password/ssh_port são gravados tanto pra method="ssh" (uso
    normal) quanto pra method="snmp" (uso opcional: credencial SSH extra
    só pra confirmar o vínculo VLAN -> porta física em devices MikroTik,
    ver collect_snmp()/collect_mikrotik_interface_details() -- se o operador
    não preencher nada aqui num device SNMP, esses campos continuam
    vazios e a coleta segue 100% via SNMP, sem essa checagem extra)."""
    cf = dict(device.custom_fields or {})
    cf["discovery_method"] = method
    if method in ("ssh", "snmp"):
        if discovery_username is not None:
            cf["discovery_username"] = discovery_username
        if discovery_password is not None:
            cf["discovery_password"] = discovery_password
        if discovery_ssh_port is not None:
            cf["discovery_ssh_port"] = discovery_ssh_port
    if method == "snmp":
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
