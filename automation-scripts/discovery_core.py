#!/usr/bin/env python3
"""
discovery_core.py
==================
Lógica compartilhada de descoberta (SSH via Netmiko e SNMP) e aplicação
no NetBox. Não tem interface própria -- é importado por:

  - discovery_netbox.py    (CLI: collect / apply via JSON em disco)
  - discovery-ui/app.py    (interface web: mesmo fluxo, mas com revisão
                             e edição na tela em vez de editar JSON)

Mantido separado do CLI de propósito, pra não duplicar a lógica de
coleta/aplicação entre os dois front-ends.
"""

import base64
import hashlib
import ipaddress
import os
import re
import subprocess
import sys

import pynetbox

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover -- ambiente sem 'cryptography' instalado
    Fernet = None
    InvalidToken = Exception

# --------------------------------------------------------------------
# Override de templates TextFSM (ntc-templates) -- alguns templates
# publicados pelo pacote têm um catch-all "-> Error" pra qualquer linha
# não prevista, o que faz UMA linha de saída fora do esperado (ex: um
# bloco de diagnóstico óptico/DWDM que aparece em interfaces de core
# como as do NE8000, "Link quality grade : GOOD") derrubar a coleta
# INTEIRA de interfaces do device com "State Error" -- confirmado contra
# device real do cliente. Os arquivos em textfsm_overrides/ são cópias
# desses templates com esse catch-all trocado por um "ignora e segue"
# (mesmo efeito prático: só não capturamos o dado daquela linha, que a
# gente não usa mesmo -- mas não quebra mais a coleta toda por causa
# dela). Aplicado copiando por cima do arquivo já instalado pelo pip,
# de forma best-effort (nunca impede o resto do módulo de carregar) e
# idempotente (roda de novo a cada import sem problema, então sobrevive
# a um `pip install --upgrade ntc-templates` que reinstale o original).
# --------------------------------------------------------------------


def _install_textfsm_overrides():
    overrides_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "textfsm_overrides")
    if not os.path.isdir(overrides_dir):
        return
    try:
        import ntc_templates

        dest_dir = os.path.join(os.path.dirname(ntc_templates.__file__), "templates")
        if not os.path.isdir(dest_dir):
            return
        for fname in os.listdir(overrides_dir):
            if not fname.endswith(".textfsm"):
                continue
            with open(os.path.join(overrides_dir, fname), "rb") as src:
                content = src.read()
            with open(os.path.join(dest_dir, fname), "wb") as dst:
                dst.write(content)
    except Exception:
        pass  # best-effort -- pior caso, volta a usar o template original do pip


_install_textfsm_overrides()

# --------------------------------------------------------------------
# Cifra da senha de descoberta (discovery_password) antes de gravar no
# NetBox -- sem isso, ela ficava salva em TEXTO PURO num custom field,
# visível pra qualquer usuário com acesso de leitura ao device no NetBox
# (UI, API ou banco). Chave derivada do FLASK_SECRET_KEY/
# DISCOVERY_UI_SECRET_KEY que já existe (gerado aleatoriamente por
# instalação pelo bootstrap.sh) -- de propósito NÃO criamos uma variável
# de ambiente nova só pra isso, pra não exigir mais um passo manual de
# configuração/redeploy. SHA-256 só normaliza esse segredo (que pode ter
# qualquer tamanho) pro tamanho exato de 32 bytes que o Fernet exige.
# --------------------------------------------------------------------
_SECRET_PREFIX = "enc:v1:"


def _fernet():
    if Fernet is None:
        return None
    raw = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("DISCOVERY_UI_SECRET_KEY")
    if not raw:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(value):
    """Cifra 'value' (ex: senha SSH) antes de gravar num custom field do
    NetBox. Marca o resultado com um prefixo de versão (_SECRET_PREFIX)
    pra distinguir de valor legado salvo em texto puro (ver
    decrypt_secret) e permitir trocar o esquema de cifra no futuro sem
    quebrar dado antigo. Best-effort: se não tiver como cifrar
    (cryptography não instalado, ou sem chave configurada), devolve
    'value' sem alteração em vez de travar o salvamento -- pior caso é
    voltar ao comportamento antigo (texto puro), não perder o dado."""
    if not value:
        return value
    f = _fernet()
    if f is None:
        return value
    return _SECRET_PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value):
    """Decifra um valor gravado por encrypt_secret(). Valor SEM o
    prefixo é tratado como texto puro legado (salvo antes dessa mudança,
    ou por uma instalação sem 'cryptography'/chave) e devolvido como
    está -- continua funcionando pra conectar via SSH, só não fica
    protegido até o operador salvar de novo ou rodar
    automation-scripts/encrypt_existing_secrets.py (migração em massa)."""
    if not value or not isinstance(value, str) or not value.startswith(_SECRET_PREFIX):
        return value
    f = _fernet()
    if f is None:
        return value
    try:
        return f.decrypt(value[len(_SECRET_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        return value


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
# ifAlias (ifXTable): a descrição de VERDADE, digitada pelo operador na
# porta (ex: "LINK-CLIENTE-XPTO"). Diferente de ifDescr (OID acima), que é
# só um texto técnico do driver/hardware da interface e, na prática, na
# maioria dos vendors sai igual ou muito parecido com o nome da porta --
# usar ifDescr como "descrição" fazia a Descrição da revisão vir com o
# nome da interface repetido sempre que não havia descrição configurada
# de verdade. ifAlias fica vazio quando o operador não configurou nada,
# que é o comportamento certo (ver collect_snmp() abaixo).
OID_IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"
OID_IF_ADMIN_STATUS = "1.3.6.1.2.1.2.2.1.7"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
# ipAddrTable (RFC1213-MIB) -- tabela clássica de IP por interface. Só
# IPv4, mas é suportada por praticamente todo device (Cisco/Huawei/
# MikroTik/Juniper), suficiente pra mostrar o IP configurado em cada
# porta na revisão. Diferente das tabelas acima (indexadas por
# ifIndex), essa é indexada pelo PRÓPRIO endereço IP -- ver
# _snmp_walk_ip_indexed().
OID_IP_AD_ENT_IF_INDEX = "1.3.6.1.2.1.4.20.1.2"
OID_IP_AD_ENT_NET_MASK = "1.3.6.1.2.1.4.20.1.3"


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


def _snmp_walk_ip_indexed(host, community, base_oid, timeout=5, retries=1):
    """Como snmp_walk(), mas pra tabelas indexadas pelo próprio endereço
    IP (ex: ipAddrTable) em vez de um índice inteiro simples (ifIndex).
    snmp_walk() pega só o ÚLTIMO componente do OID como índice, o que
    pra essas tabelas pegaria só o último octeto do IP (ex: '1' em vez
    de '10.0.0.1') -- aqui pegamos os 4 últimos componentes (o IPv4
    completo) como chave."""
    cmd = _snmp_cmd("snmpwalk", host, community, base_oid, timeout, retries)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout * (retries + 1) + 15)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "snmpwalk falhou (sem stderr)")
    out = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        oid_full, _, value = line.partition(" ")
        parts = oid_full.split(".")
        ip = ".".join(parts[-4:])  # últimos 4 componentes = o IPv4 em si
        out[ip] = value.strip().strip('"')
    return out


def _netmask_to_prefixlen(netmask):
    """'255.255.255.0' -> 24. None se não conseguir parsear."""
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{netmask}").prefixlen
    except Exception:
        return None


def _collect_snmp_interface_ips(host, community, timeout, retries):
    """IPs configurados em cada interface (RFC1213 ipAddrTable), já em
    notação CIDR -- retorna {ifindex: ['x.x.x.x/yy', ...]}. Best-effort:
    quem chama trata falha/tabela ausente como opcional (nem todo device
    responde essa tabela, embora seja bem padrão)."""
    ip_if_index = _snmp_walk_ip_indexed(host, community, OID_IP_AD_ENT_IF_INDEX, timeout, retries)
    ip_netmask = _snmp_walk_ip_indexed(host, community, OID_IP_AD_ENT_NET_MASK, timeout, retries)
    by_ifindex = {}
    for ip, ifidx in ip_if_index.items():
        prefixlen = _netmask_to_prefixlen(ip_netmask.get(ip))
        cidr = f"{ip}/{prefixlen}" if prefixlen is not None else ip
        by_ifindex.setdefault(ifidx, []).append(cidr)
    return by_ifindex


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
        # ifAlias pode não existir em devices sem ifXTable completo -- não
        # é erro, só fica sem descrição nenhuma pra essas portas (melhor
        # que preencher com o nome cru da interface, que é o que ifDescr
        # ia dar).
        try:
            if_alias = snmp_walk(host, community, OID_IF_ALIAS, timeout, retries)
        except Exception:
            if_alias = {}
        admin_status = snmp_walk(host, community, OID_IF_ADMIN_STATUS, timeout, retries)
        oper_status = snmp_walk(host, community, OID_IF_OPER_STATUS, timeout, retries)
        # IP configurado em cada porta (ipAddrTable) -- opcional, nem
        # todo device responde essa tabela (ex: alguns firmwares OLT),
        # então uma falha aqui não derruba o resto da coleta SNMP.
        try:
            ips_by_ifindex = _collect_snmp_interface_ips(host, community, timeout, retries)
        except Exception:
            ips_by_ifindex = {}

        interfaces = []
        for idx, descr in if_descr.items():
            interfaces.append(
                {
                    "index": idx,
                    "name": if_name.get(idx) or descr,
                    # ifAlias = descrição configurada pelo operador de
                    # verdade (ver comentário no OID_IF_ALIAS acima) --
                    # vazio quando não tem nada configurado, em vez de
                    # repetir o nome/descrição técnica da porta.
                    "descr": if_alias.get(idx) or "",
                    "admin_status": _normalize_status(admin_status.get(idx)),
                    "oper_status": _normalize_status(oper_status.get(idx)),
                    "ip_addresses": ips_by_ifindex.get(idx, []),
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
    - '/interface pppoe-client print terse': mesmo caso da VLAN -- uma
      interface pppoe-client (ex: "pppoe-out1") também tem uma porta
      física "por trás" dela (campo "interface="), só que esse vínculo
      também só aparece nessa consulta específica de PPPoE, não no
      "/interface print terse" genérico. Consulta separada e opcional:
      se o device não tiver nenhum client PPPoE configurado (ou a
      versão do RouterOS não reconhecer o comando), a saída vem vazia
      ou dá erro -- não afeta o resto (comment/VLAN já coletados acima).

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
        try:
            pppoe_output = conn.send_command("/interface pppoe-client print terse without-paging")
        except Exception:
            pppoe_output = ""
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

    for entry in _ros_split_entries(pppoe_output or ""):
        name = _ros_terse_value(entry, "name")
        if not name:
            continue
        parent = _ros_terse_value(entry, "interface")
        details.setdefault(name, {"comment": None, "parent": None})
        if parent:
            details[name]["parent"] = parent

    return details


# --------------------------------------------------------------------
# SSH (Netmiko puro -- sem NAPALM, por decisão do projeto: um único
# caminho de coleta via SSH em vez de dois mecanismos diferentes
# (NAPALM pra fabricantes com driver, Netmiko/CLI só pro MikroTik como
# extra). Usa TextFSM (pacote ntc-templates, carregado automaticamente
# pelo Netmiko quando use_textfsm=True) pra estruturar a saída dos
# comandos "show"/"display" -- evita reinventar um parser manual por
# fabricante, trabalho que bibliotecas mantidas pela comunidade (essa,
# ou o próprio NAPALM antes) já resolvem de forma testada.
#
# Resolve o device_type do Netmiko a partir do Fabricante (Device Type >
# Manufacturer) em vez de exigir que o operador escolha a Platform
# manualmente pra cada device -- mesma tabela de regras usada pela
# sugestão automática no dashboard (MANUFACTURER_RULES, dashboard.html),
# só que aqui do lado do servidor, pra funcionar mesmo sem a Platform
# setada no NetBox. Se o device já tiver uma Platform explícita, ela
# continua tendo prioridade (permite override manual pra casos fora do
# padrão -- o Slug precisa ser um device_type válido do Netmiko, ex:
# 'cisco_ios', não mais um driver NAPALM).
# --------------------------------------------------------------------
_MANUFACTURER_SSH_RULES = [
    (re.compile(r"cisco", re.I), "cisco_ios"),
    (re.compile(r"juniper", re.I), "juniper_junos"),
    (re.compile(r"arista", re.I), "arista_eos"),
    (re.compile(r"palo\s*alto", re.I), "paloalto_panos"),
    # "huawei_vrp" (o sistema operacional VRP dos roteadores/switches
    # Huawei), NÃO o device_type genérico "huawei" do Netmiko -- os
    # templates TextFSM do ntc-templates pra Huawei só existem com o
    # prefixo "huawei_vrp_*" (ex: huawei_vrp_display_interface.textfsm).
    # Usar "huawei" aqui faz o Netmiko conectar normalmente, mas o
    # send_command(..., use_textfsm=True) nunca acha template nenhum
    # (confirmado contra device real do cliente: 'display interface'/
    # 'display version' voltavam sem dado estruturado nenhum).
    (re.compile(r"huawei", re.I), "huawei_vrp"),
    # "datacom_dmos" NÃO é um device_type de verdade do Netmiko (não
    # existe driver Datacom/DmOS lá, nem template no ntc-templates) --
    # é só um marcador interno que faz collect_ssh() desviar pra
    # _collect_datacom() (parser próprio, sem TextFSM) em vez do
    # caminho genérico via _SSH_COMMANDS/_ssh_collect_textfsm.
    (re.compile(r"datacom", re.I), "datacom_dmos"),
]
_OLT_PATTERN = re.compile(r"\bolt\b|ma5\d{3}", re.I)

# Comando de interfaces + comando de "facts" (serial/versão) por
# device_type do Netmiko -- ambos rodados com use_textfsm=True. Não
# existe um "show interfaces" universal entre fabricantes, e mesmo os
# nomes dos CAMPOS retornados variam de template pra template -- por
# isso _pick() abaixo tenta várias chaves candidatas em vez de assumir
# um nome fixo.
_SSH_COMMANDS = {
    "cisco_ios": {"interfaces": "show interfaces", "version": "show version"},
    "arista_eos": {"interfaces": "show interfaces", "version": "show version"},
    "juniper_junos": {"interfaces": "show interfaces terse", "version": "show version"},
    "paloalto_panos": {"interfaces": "show interface all", "version": "show system info"},
    "huawei_vrp": {"interfaces": "display interface", "version": "display version"},
}


def resolve_ssh_device_type(manufacturer, device_type_model=None):
    """Adivinha o device_type do Netmiko a partir do nome do Fabricante
    (substitui o antigo resolve_platform_slug()/driver NAPALM). Retorna
    None se não reconhecer o fabricante, ou se for um Huawei tipo OLT
    (esses não respondem aos comandos VRP normais de forma confiável --
    usam SNMP, ver MANUFACTURER_RULES/oltCheck em dashboard.html, mesma
    lógica de antes)."""
    manufacturer = manufacturer or ""
    for pattern, device_type in _MANUFACTURER_SSH_RULES:
        if pattern.search(manufacturer):
            if device_type == "huawei_vrp" and _OLT_PATTERN.search(device_type_model or ""):
                return None
            return device_type
    return None


def _device_manufacturer_info(device):
    """(manufacturer, device_type_model) como string, best-effort -- usado
    só pra alimentar resolve_ssh_device_type()."""
    manufacturer = None
    device_type_model = None
    try:
        if device.device_type:
            device_type_model = str(device.device_type)
            if getattr(device.device_type, "manufacturer", None):
                manufacturer = str(device.device_type.manufacturer)
    except Exception:
        pass
    return manufacturer, device_type_model


def _pick(row, *keys):
    """Pega o primeiro valor não vazio de 'row' (um dict vindo de uma
    linha parseada por TextFSM) tentando várias chaves candidatas -- os
    nomes de campo variam de template pra template do ntc-templates (ex:
    MAC pode vir como MAC_ADDRESS, ADDRESS ou BIA dependendo do
    fabricante), então não dá pra assumir um nome fixo.

    Tenta a chave exata primeiro, depois em minúsculo -- o Netmiko (via
    netmiko.utilities.clitable_to_dict(), confirmado lendo o código
    instalado: `temp_dict[cli_table.header[index].lower()] = element`)
    baixa pra minúsculo o nome de TODO campo do TextFSM antes de devolver
    o dict pro send_command(..., use_textfsm=True) -- os templates deste
    projeto (e do ntc-templates em geral) declaram os campos em
    MAIÚSCULO ("Value INTERFACE ...", "Value ETH_TRUNK ..."), então sem
    esse fallback toda chamada _pick(row, "INTERFACE", ...) SEMPRE
    retornava None nessa versão do Netmiko -- o que também zerava
    silenciosamente a lista de interfaces inteira em
    _ssh_collect_textfsm() (só não ficava óbvio em devices com método
    'both', porque o SNMP preenchia por baixo)."""
    for key in keys:
        value = row.get(key)
        if value is None:
            value = row.get(key.lower())
        if isinstance(value, list):
            value = value[0] if value else None
        if value:
            return str(value).strip()
    return None


def _ssh_collect_textfsm(conn, iface_cmd, version_cmd):
    """Coleta via TextFSM (Netmiko + ntc-templates) -- usada pros
    fabricantes com device_type reconhecido aqui (Cisco/Arista/Juniper/
    Palo Alto/Huawei). Normaliza os nomes de campo (que variam entre
    templates) pro formato comum usado no resto do projeto (name/descr/
    admin_status/oper_status/mac_address/ip_addresses).

    Retorna (interfaces, serial, os_version, errors). 'errors' fica
    populado sem levantar exceção quando um comando não tem template
    TextFSM disponível pro device_type em questão -- nesse caso o
    Netmiko devolve a STRING crua em vez de uma lista, e a gente avisa o
    operador em vez de silenciosamente devolver uma lista vazia sem
    explicação nenhuma."""
    errors = []
    interfaces = []

    try:
        parsed = conn.send_command(iface_cmd, use_textfsm=True)
    except Exception as exc:
        return [], None, None, [f"falha ao rodar '{iface_cmd}': {exc}"]

    if not isinstance(parsed, list):
        errors.append(
            f"'{iface_cmd}' não tem um template TextFSM disponível pra esse "
            f"device (saída não veio estruturada) -- sem dados de interface, "
            f"revise manualmente"
        )
        parsed = []

    for row in parsed:
        name = _pick(row, "INTERFACE", "PORT", "NAME")
        if not name:
            continue
        status_raw = (_pick(row, "LINK_STATUS", "STATUS", "ADMIN_STATE", "PHY", "ADMIN_STATUS") or "").lower()
        proto_raw = (_pick(row, "PROTOCOL_STATUS", "PROTOCOL", "LINE_PROTOCOL", "LINK_STATE") or "").lower()
        admin_down = any(word in status_raw for word in ("down", "disable", "shutdown"))
        oper_up = "up" in proto_raw or ("up" in status_raw and not admin_down)
        ip_raw = _pick(row, "IP_ADDRESS", "IP", "ADDRESS_IP", "PRIMARY_IP")
        ip_addresses = []
        if ip_raw and ip_raw.lower() not in ("unassigned", "none", "-", "n/a"):
            ip_addresses = [ip_raw]
        interfaces.append({
            "name": name,
            "descr": _pick(row, "DESCRIPTION", "DESC") or "",
            "admin_status": "down" if admin_down else "up",
            "oper_status": "up" if oper_up else "down",
            "mac_address": _pick(row, "MAC_ADDRESS", "ADDRESS", "BIA", "HARDWARE_ADDR"),
            "ip_addresses": ip_addresses,
        })

    serial = None
    os_version = None
    try:
        vparsed = conn.send_command(version_cmd, use_textfsm=True)
    except Exception as exc:
        vparsed = None
        errors.append(f"falha ao rodar '{version_cmd}': {exc}")

    if isinstance(vparsed, list) and vparsed:
        vrow = vparsed[0]
        serial = _pick(vrow, "SERIAL", "SERIAL_NUMBER", "HARDWARE")
        os_version = _pick(vrow, "VERSION", "OS_VERSION", "SOFTWARE_VERSION", "RUNNING_IMAGE", "SW_VERSION")
    elif vparsed is not None:
        errors.append(
            f"'{version_cmd}' não tem template TextFSM disponível pra esse "
            f"device -- sem serial/versão"
        )

    return interfaces, serial, os_version, errors


def _huawei_collect_eth_trunk(conn):
    """Roda 'display eth-trunk' (sem ID -- lista TODOS os trunks de uma
    vez) e devolve {nome_da_porta_membro: nome_do_trunk} (ex:
    {'GigabitEthernet0/0/1': 'Eth-Trunk1'}) -- usado pra popular
    lag_parent nas interfaces físicas já coletadas. Best-effort:
    qualquer falha (comando não existe nessa versão, sem trunk
    configurado, TextFSM sem template) só devolve dict vazio, nunca
    derruba a coleta principal de interfaces (que já está pronta quando
    isso é chamado).

    Validado contra saída real de 'display eth-trunk' de device do
    cliente (VS-BGP-Futuretec)."""
    try:
        parsed = conn.send_command("display eth-trunk", use_textfsm=True)
    except Exception:
        return {}
    if not isinstance(parsed, list):
        return {}
    members = {}
    for row in parsed:
        trunk = _pick(row, "ETH_TRUNK")
        member = _pick(row, "INTERFACE")
        # A porta ATIVA como "reference port" da trunk vem anotada como
        # "GigabitEthernet0/1/1(r)" (e a de maior prioridade como
        # "...(h)") -- confirmado contra device real. Sem tirar esse
        # sufixo, a chave desse dict nunca bate com o nome de verdade da
        # interface (coletado por 'display interface', sem anotação
        # nenhuma), e essa porta específica fica sem lag_parent.
        if member:
            member = re.sub(r"\([a-z]\)$", "", member)
        if trunk and member:
            members[member] = trunk
    return members


# --------------------------------------------------------------------
# Datacom (DmOS) -- sem device_type nativo no Netmiko e sem template no
# ntc-templates (confirmado: nenhum dos dois tem suporte a Datacom/DmOS
# hoje), então a coleta aqui é por conta própria: conecta com o driver
# genérico do Netmiko (detecção de prompt sem nenhum comando de
# desabilitar paginação específico -- ainda sem confirmação se é
# necessário em saída muito longa) e parseia a saída de dois comandos
# com regex em vez de TextFSM. Parsers validados contra saída real de
# um switch DmOS do cliente (prompt "SW.CORE-DTC#").
# --------------------------------------------------------------------
_DATACOM_SECTION_RE = re.compile(r"^(.+?)\s+Interfaces:\s*$")
_DATACOM_ROW_RE = re.compile(r"^(\d+/\d+/\d+)\s+(.*)$")


def _datacom_iface_prefix(section_title):
    """'Ten Gigabit Ethernet' -> 'ten-gigabit-ethernet' -- mesmo formato
    usado pelo próprio equipamento em 'show interface utilization'
    (confirmado contra device real: ten-gigabit-ethernet-1/1/1,
    forty-gigabit-ethernet-1/1/1)."""
    return re.sub(r"\s+", "-", section_title.strip().lower())


def _datacom_parse_description(output):
    """Parseia 'show interface description' -> {nome_completo: descrição}.
    A tabela tem seções por tipo de porta (Ten/Forty Gigabit Ethernet),
    cada uma com seu próprio cabeçalho "<Tipo> Interfaces:" que define o
    prefixo do nome -- ignora qualquer linha que não seja um desses dois
    formatos (cabeçalho de coluna, separador tracejado, prompt) em vez
    de tentar reconhecer cada um deles."""
    result = {}
    prefix = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _DATACOM_SECTION_RE.match(line)
        if m:
            prefix = _datacom_iface_prefix(m.group(1))
            continue
        m = _DATACOM_ROW_RE.match(line)
        if m and prefix:
            port_id, descr = m.group(1), m.group(2).strip()
            result[f"{prefix}-{port_id}"] = descr
    return result


def _datacom_parse_link(output):
    """Parseia 'show interface link' -> lista de dicts com nome completo,
    status admin/oper e LAG pai (coluna 'Parent LAG', ex: 'lag-1') --
    mesma lógica de seção por prefixo do parser acima."""
    interfaces = []
    prefix = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _DATACOM_SECTION_RE.match(line)
        if m:
            prefix = _datacom_iface_prefix(m.group(1))
            continue
        if not prefix or not re.match(r"^\d+/\d+/\d+\s", line):
            continue
        # Colunas de largura fixa separadas por 2+ espaços: ID, Link,
        # Shutdown, Speed, Duplex, Disabled by, Blocked by, Parent LAG,
        # [Description]. A descrição aqui vem TRUNCADA em nome muito
        # longo (ex: "PTP-OLT-POP-CIDAD...") -- 'show interface
        # description' é a fonte confiável pra descrição completa, essa
        # aqui só serve de apoio se a outra não trouxer nada pra essa
        # porta (ver _collect_datacom).
        cols = re.split(r"\s{2,}", line)
        if len(cols) < 8:
            continue
        port_id, link, shutdown, speed, duplex, disabled_by, blocked_by, lag = cols[:8]
        descr_fallback = cols[8] if len(cols) > 8 else ""
        interfaces.append({
            "name": f"{prefix}-{port_id}",
            "oper_status": "up" if link.strip().lower() == "up" else "down",
            "admin_status": "down" if shutdown.strip().lower() == "true" else "up",
            "lag_parent": lag if lag and lag != "-" else None,
            "descr_fallback": descr_fallback,
        })
    return interfaces


def _collect_datacom(device, username, password, port=None):
    """Coleta Datacom DmOS via SSH -- 'show interface description' (nome
    completo + descrição sem truncar) e 'show interface link' (status +
    LAG pai), sem TextFSM (não existe template pra esse fabricante)."""
    if not device.primary_ip4:
        return {"method": "ssh", "errors": ["sem IP de gerência (primary_ip4)"]}

    host = str(device.primary_ip4).split("/")[0]
    data = {"method": "ssh", "host": host, "errors": []}

    from netmiko import ConnectHandler, NetmikoAuthenticationException, NetmikoTimeoutException

    try:
        conn = ConnectHandler(
            device_type="generic_ssh",
            host=host,
            username=username,
            password=password,
            port=int(port) if port else 22,
            timeout=15,
        )
    except NetmikoAuthenticationException:
        data["errors"].append("falha ao conectar: usuário/senha rejeitados pelo device")
        return data
    except NetmikoTimeoutException:
        data["errors"].append("falha ao conectar: timeout (IP/porta inacessível ou SSH não habilitado)")
        return data
    except Exception as exc:
        data["errors"].append(f"falha ao conectar: {exc}")
        return data

    descr_by_name = {}
    link_interfaces = []
    try:
        try:
            descr_out = conn.send_command("show interface description")
            descr_by_name = _datacom_parse_description(descr_out)
        except Exception as exc:
            data["errors"].append(f"falha ao rodar 'show interface description': {exc}")
        try:
            link_out = conn.send_command("show interface link")
            link_interfaces = _datacom_parse_link(link_out)
        except Exception as exc:
            data["errors"].append(f"falha ao rodar 'show interface link': {exc}")
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass

    interfaces = []
    seen = set()
    for iface in link_interfaces:
        name = iface["name"]
        seen.add(name)
        interfaces.append({
            "name": name,
            "descr": descr_by_name.get(name) or iface.get("descr_fallback") or "",
            "admin_status": iface["admin_status"],
            "oper_status": iface["oper_status"],
            "mac_address": None,
            "ip_addresses": [],
            "lag_parent": iface.get("lag_parent"),
        })
    # Porta que apareceu em 'description' mas não em 'link' (não deveria
    # acontecer -- os dois comandos listam as mesmas portas -- mas não
    # custa não perder o dado se acontecer).
    for name, descr in descr_by_name.items():
        if name not in seen:
            interfaces.append({
                "name": name, "descr": descr, "admin_status": "unknown",
                "oper_status": "unknown", "mac_address": None, "ip_addresses": [],
                "lag_parent": None,
            })

    if not interfaces and not data["errors"]:
        data["errors"].append(
            "nenhuma interface reconhecida na saída dos comandos -- confira "
            "se o formato do DmOS bate com o esperado"
        )

    data["serial"] = None
    data["os_version"] = None
    data["interfaces"] = interfaces
    return data


def collect_ssh(device, username, password, port=None):
    device_type = device.platform.slug if device.platform else None
    if not device_type:
        manufacturer, device_type_model = _device_manufacturer_info(device)
        device_type = resolve_ssh_device_type(manufacturer, device_type_model)
    if not device_type:
        return {
            "method": "ssh",
            "errors": [
                "não consegui adivinhar como conectar via SSH pelo Fabricante -- "
                "confirme se o Device Type tem um Fabricante reconhecido "
                "(Cisco/Juniper/Arista/Palo Alto/Huawei/Datacom) ou defina a "
                "Platform manualmente no device (NetBox > Devices > editar, "
                "com o Slug igual ao device_type do Netmiko, ex: 'cisco_ios')"
            ],
        }
    if not device.primary_ip4:
        return {"method": "ssh", "errors": ["sem IP de gerência (primary_ip4)"]}

    if device_type == "datacom_dmos":
        return _collect_datacom(device, username, password, port=port)

    commands = _SSH_COMMANDS.get(device_type)
    if not commands:
        return {
            "method": "ssh",
            "errors": [f"device_type '{device_type}' não tem comandos configurados aqui"],
        }

    host = str(device.primary_ip4).split("/")[0]
    data = {"method": "ssh", "host": host, "errors": []}

    from netmiko import ConnectHandler, NetmikoAuthenticationException, NetmikoTimeoutException

    try:
        conn = ConnectHandler(
            device_type=device_type,
            host=host,
            username=username,
            password=password,
            port=int(port) if port else 22,
            timeout=15,
        )
    except NetmikoAuthenticationException:
        data["errors"].append("falha ao conectar: usuário/senha rejeitados pelo device")
        return data
    except NetmikoTimeoutException:
        data["errors"].append("falha ao conectar: timeout (IP/porta inacessível ou SSH não habilitado)")
        return data
    except Exception as exc:
        data["errors"].append(f"falha ao conectar: {exc}")
        return data

    try:
        interfaces, serial, os_version, errors = _ssh_collect_textfsm(
            conn, commands["interfaces"], commands["version"]
        )
        # Vínculo de Eth-Trunk (LAG) -- só pra Huawei, best-effort, roda
        # na MESMA sessão SSH antes de desconectar (ver
        # _huawei_collect_eth_trunk). Falha aqui nunca derruba a coleta
        # de interfaces, que já está pronta nesse ponto.
        if device_type == "huawei_vrp":
            trunk_members = _huawei_collect_eth_trunk(conn)
            if trunk_members:
                for iface in interfaces:
                    trunk = trunk_members.get(iface["name"])
                    if trunk:
                        iface["lag_parent"] = trunk
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass

    data["errors"].extend(errors)
    data["serial"] = serial
    data["os_version"] = os_version
    data["interfaces"] = interfaces
    return data


def _merge_interface(snmp_if, ssh_if):
    """Combina os dados de uma mesma interface vindos de SNMP e de SSH
    (método 'both') -- o SSH (via TextFSM, ver _ssh_collect_textfsm)
    costuma trazer descrição, MAC e status mais confiáveis (lido direto
    do comando do fabricante, sem depender de OID padrão), então tem
    prioridade quando o SSH trouxe algo; o que o SSH não trouxe (ou se
    o SSH falhou nessa interface específica) continua vindo do SNMP,
    que serve de rede de segurança."""
    base = dict(snmp_if or ssh_if or {})
    # lag_parent só existe do lado do SSH (Eth-Trunk no Huawei via
    # _huawei_collect_eth_trunk, coluna Parent-LAG no Datacom via
    # _collect_datacom -- SNMP não tem esse dado) -- sem essa chave aqui,
    # 'base' (que começa como cópia do lado SNMP quando a interface
    # aparece nos dois) nunca ganhava o valor detectado via SSH, e o
    # campo LAG chegava sempre vazio na revisão pra qualquer device
    # coletado com o método "both" (confirmado: bug reportado pelo
    # operador em device Datacom E Huawei, ambos usando esse método).
    for key in ("descr", "admin_status", "oper_status", "mac_address", "lag_parent"):
        ssh_val = (ssh_if or {}).get(key)
        if ssh_val:
            base[key] = ssh_val
    # IPs: aqui não é "SSH sobrescreve SNMP" como acima -- os dois lados
    # são fontes igualmente válidas do mesmo fato (IP configurado na
    # porta), então juntamos os dois (união, sem duplicar) em vez de um
    # substituir o outro.
    ssh_ips = (ssh_if or {}).get("ip_addresses") or []
    snmp_ips = (snmp_if or {}).get("ip_addresses") or []
    merged_ips = list(dict.fromkeys(list(ssh_ips) + list(snmp_ips)))
    if merged_ips:
        base["ip_addresses"] = merged_ips
    return base


def collect_both(device, credentials, snmp_timeout=5, snmp_retries=1):
    """Roda SSH (Netmiko) e SNMP na MESMA coleta e cruza os resultados por
    interface -- pensado pra device onde nenhum dos dois métodos sozinho
    é suficiente (ex: um fabricante cujo comando SSH não traz certos
    campos mas o SNMP traz, ou vice-versa). Cada protocolo roda de forma
    independente e best-effort: se um falhar (ex: fabricante sem
    device_type SSH reconhecido aqui), a coleta segue com o que o outro
    trouxe em vez de falhar tudo -- os erros de ambos ficam registrados
    em 'errors' pro operador ver o que não funcionou."""
    username = credentials.get("discovery_username")
    password = credentials.get("discovery_password")
    port = credentials.get("discovery_ssh_port")
    community = credentials.get("discovery_snmp_community")

    missing = []
    if not username or not password:
        missing.append("usuário/senha (SSH)")
    if not community:
        missing.append("community (SNMP)")
    if missing:
        return {"method": "both", "errors": [f"falta {' e '.join(missing)}"]}

    ssh_data = collect_ssh(device, username, password, port=port)
    snmp_data = collect_snmp(
        device, community, timeout=snmp_timeout, retries=snmp_retries,
        ssh_username=username, ssh_password=password, ssh_port=port,
    )

    data = {
        "method": "both",
        "host": snmp_data.get("host") or ssh_data.get("host"),
        "errors": list(ssh_data.get("errors") or []) + list(snmp_data.get("errors") or []),
        "sys_name": snmp_data.get("sys_name"),
        "sys_descr": snmp_data.get("sys_descr"),
        "serial": ssh_data.get("serial"),
        "os_version": ssh_data.get("os_version"),
    }

    ssh_by_norm = {normalize_ifname(i["name"]): i for i in ssh_data.get("interfaces", []) if i.get("name")}
    snmp_by_norm = {normalize_ifname(i["name"]): i for i in snmp_data.get("interfaces", []) if i.get("name")}

    interfaces = []
    for norm in set(ssh_by_norm) | set(snmp_by_norm):
        snmp_if = snmp_by_norm.get(norm)
        ssh_if = ssh_by_norm.get(norm)
        # Nome "oficial" prioriza o que veio do SNMP (ifName/ifDescr) --
        # só usa o nome do SSH se essa interface não apareceu no SNMP.
        merged = _merge_interface(snmp_if, ssh_if)
        merged["name"] = (snmp_if or ssh_if)["name"]
        interfaces.append(merged)

    data["interfaces"] = interfaces
    return data


def collect_device(device, method, credentials, snmp_timeout=5, snmp_retries=1):
    """Ponto de entrada único: despacha pro coletor certo a partir do
    'discovery_method' + dict de credenciais (mesmas chaves dos custom
    fields: username/password ou community).

    Ponto único também pra decifrar discovery_password (ver
    encrypt_secret/decrypt_secret) -- tanto o CLI (discovery_netbox.py)
    quanto a interface web (discovery-ui/app.py) chamam só essa função
    pra coletar, nunca collect_ssh()/collect_snmp()/collect_both()
    direto, então decifrar aqui (numa CÓPIA do dict, não mexe no
    original) cobre os dois front-ends sem duplicar a chamada em cada
    um."""
    credentials = dict(credentials or {})
    if credentials.get("discovery_password"):
        credentials["discovery_password"] = decrypt_secret(credentials["discovery_password"])
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
    elif method == "both":
        return collect_both(device, credentials, snmp_timeout=snmp_timeout, snmp_retries=snmp_retries)
    else:
        return {"method": method, "errors": [f"método desconhecido: '{method}' (use 'ssh', 'snmp' ou 'both')"]}


# --------------------------------------------------------------------
# Normalização de nome de interface -- necessário porque o Device Type
# no NetBox costuma já vir com um Interface Template (ex: NE8000 com
# 48 portas pré-cadastradas como "GigabitEthernet0/0/1"), mas o que
# volta da descoberta (SSH via Netmiko/TextFSM, ou SNMP via ifDescr/ifName) pode
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
# SSH via Netmiko) não traz esse dado de forma confiável, então tentamos
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


# --------------------------------------------------------------------
# Ordem usada pra AGRUPAR as interfaces na tela de revisão -- físicas
# primeiro (por velocidade crescente), depois LAG/bridge, e
# virtual/VLAN por último (normalmente é sub-interface "por cima" de
# uma porta física, faz sentido aparecer depois dela na lista). Não é
# a mesma ordem de INTERFACE_TYPE_CHOICES (que é só a ordem das opções
# do dropdown "Tipo" e prioriza "virtual" primeiro por ser o tipo mais
# comum de sub-interface/VLAN nesse projeto).
# --------------------------------------------------------------------
INTERFACE_TYPE_SORT_ORDER = [
    "100base-tx",
    "1000base-t",
    "2.5gbase-t",
    "5gbase-t",
    "10gbase-t",
    "1000base-x-sfp",
    "10gbase-x-sfpp",
    "25gbase-x-sfp28",
    "40gbase-x-qsfpp",
    "100gbase-x-qsfp28",
    "lag",
    "bridge",
    "virtual",
    "other",
]
_INTERFACE_TYPE_SORT_INDEX = {t: i for i, t in enumerate(INTERFACE_TYPE_SORT_ORDER)}


def _natural_sort_key(name):
    """Quebra o nome em pedaços texto/número pra ordenar 'Gi0/0/2' antes
    de 'Gi0/0/10' -- ordenação alfabética pura colocaria o '10' antes
    do '2'."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", name or "")]


def interface_sort_key(iface):
    """Chave de ordenação usada na tela de revisão -- agrupa por tipo
    (usa guessed_type se já calculado, senão chuta na hora) e, dentro
    do mesmo tipo, ordena pelo nome de forma natural/numérica."""
    itype = iface.get("guessed_type") or guess_interface_type(iface.get("name"))
    type_idx = _INTERFACE_TYPE_SORT_INDEX.get(itype, len(INTERFACE_TYPE_SORT_ORDER))
    return (type_idx, _natural_sort_key(iface.get("name")))


def guess_interface_type(name):
    """Chute do tipo NetBox a partir do nome da interface descoberta.
    Best-effort -- sempre revisável na tela de revisão antes de gravar.

    Cobre tanto a nomenclatura Cisco-like (GigabitEthernet, TenGigabitEthernet,
    Port-channel...) quanto a nomenclatura Huawei VRP, confirmada contra
    device real: "Eth-Trunk" (LAG, mas tratado como Virtual -- ver
    comentário abaixo), "XGigabitEthernet" (10GE/SFP+ -- NÃO confundir com
    GigabitEthernet comum, o "X" na frente muda tudo), "100GE"/"40GE"
    (QSFP28/QSFP+), "InLoopBack" (variação de nome do loopback) -- e a
    nomenclatura Datacom DmOS, com o número da velocidade por extenso em
    vez de dígitos (ex: "ten-gigabit-ethernet-1/1/1",
    "forty-gigabit-ethernet-1/1/1" -- ver _datacom_iface_prefix() acima,
    que gera esse formato a partir do cabeçalho "Ten/Forty/... Gigabit
    Ethernet Interfaces:" do próprio equipamento)."""
    n = (name or "").strip().lower()
    if not n:
        return "other"
    # "Eth-Trunk" do Huawei é tecnicamente um LAG (agregação), mas por
    # pedido explícito do cliente é tratado como Virtual aqui -- se quiser
    # o tipo "lag" de verdade (com o agrupamento de portas físicas que o
    # NetBox faz pra LAG), troca esse "virtual" por "lag" nessa linha.
    if n.startswith(("vlan", "vl", "eoip", "tunnel", "gre", "eth-trunk")) or "loopback" in n:
        return "virtual"
    # Sub-interface dotted-notation (Gi0/1.100, ge-0/0/1.100...) é sempre
    # uma interface lógica de VLAN por cima de uma física -- isso tem que
    # ser checado ANTES do match de prefixo físico logo abaixo, senão
    # "gi0/1.100" cai em 1000base-t só por começar com "gi" (o NetBox só
    # deixa vincular "parent" em interface type=virtual).
    if "." in n and n.rsplit(".", 1)[-1].isdigit():
        return "virtual"
    if n.startswith("bridge") or n == "br" or n.startswith("br-"):
        return "bridge"
    if n.startswith(("port-channel", "portchannel", "po", "lag", "bond")):
        return "lag"
    # Datacom DmOS escreve a velocidade por extenso ("ten-", "forty-",
    # "hundred-", "twenty-five-") em vez de dígitos -- checado ANTES dos
    # padrões numéricos abaixo (100G/40G/25G/10G), maior primeiro, pra
    # "hundred-gigabit-ethernet" não cair sem querer em "gigabit-ethernet"
    # genérico (1G) só por conter esse pedaço no meio do nome.
    if n.startswith("hundred-gigabit-ethernet"):
        return "100gbase-x-qsfp28"
    if n.startswith("forty-gigabit-ethernet"):
        return "40gbase-x-qsfpp"
    if n.startswith(("twenty-five-gigabit-ethernet", "twentyfive-gigabit-ethernet")):
        return "25gbase-x-sfp28"
    if n.startswith("ten-gigabit-ethernet"):
        return "10gbase-x-sfpp"
    # 100G/40G verificado ANTES de 10G -- Huawei "100GE0/0/1" não pode
    # cair na regra de 10G por engano.
    if n.startswith(("100ge", "100gb")) or "100gbase" in n:
        return "100gbase-x-qsfp28"
    if n.startswith(("qsfp", "40ge", "40gb")) or "40gbase" in n:
        return "40gbase-x-qsfpp"
    if (
        "sfpplus" in n or "sfp-plus" in n
        or n.startswith(("te", "xe", "xgigabitethernet", "xge", "10ge"))
        or "tengig" in n
    ):
        return "10gbase-x-sfpp"
    if "sfp" in n:
        return "1000base-x-sfp"
    if n.startswith("fa") or "fastethernet" in n:
        return "100base-tx"
    # "gigabit-ethernet-1/1/1" (Datacom, porta de 1G -- sem "ten-"/
    # "forty-"/... na frente, já eliminados pelos checks acima) cai aqui
    # igual ao "gigabitethernet" (Cisco, sem hífen).
    if n.startswith(("ether", "gi", "ge", "eth")) or "gigabitethernet" in n or "gigabit-ethernet" in n:
        return "1000base-t"
    return "other"


def is_vlan_ifname(name):
    """True se o nome parece uma interface lógica que faz sentido vincular
    a uma porta física na revisão -- cobre o padrão MikroTik (vlanN, sem
    relação de nome com a porta física), o padrão dotted-notation usado
    por Cisco/Juniper/Huawei (ex: 'Gi0/1.100', 'ge-0/0/1.100', onde o
    sufixo depois do ponto é o ID da VLAN) e pppoe-client do MikroTik
    (ex: 'pppoe-out1' -- não é VLAN de verdade, mas tem o mesmo conceito
    de "porta física por trás", confirmado via '/interface pppoe-client
    print terse' em collect_mikrotik_interface_details())."""
    n = (name or "").strip().lower()
    if n.startswith("vlan") or n.startswith("vl"):
        return True
    if n.startswith("pppoe"):
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


def _sync_interface_ip(nb, nb_if, ip_str, device_id, description=""):
    """Garante que 'ip_str' (CIDR, ex: '10.0.0.1/24') exista no IPAM e
    esteja associado à interface 'nb_if' -- mesmo padrão de busca já
    usado em set_primary_ip() (tenta achar por endereço+device antes de
    achar só por endereço, antes de criar novo, pra não duplicar o
    mesmo IP como objetos diferentes no NetBox). Se o IP já existir
    associado a outra interface/device, REASSOCIA pra essa interface --
    o que a descoberta encontrou agora na porta é tratado como fonte de
    verdade de onde aquele IP está fisicamente hoje.

    'description' é a mesma descrição já capturada/editada pra interface
    dona desse IP (ver apply_device_result) -- os equipamentos não
    expõem uma descrição por IP separada da descrição da porta, então
    reaproveitar é a única fonte que faz sentido; sem isso, todo IP
    Address criado no IPAM ficava sem descrição nenhuma, exigindo abrir
    cada device pra saber pra que serve aquele endereço.

    Retorna True se criou ou mudou algo, False se já estava certo."""
    ip_str = (ip_str or "").strip()
    if not ip_str:
        return False
    if "/" not in ip_str:
        ip_str = f"{ip_str}/32"
    description = description or ""

    ip_obj = nb.ipam.ip_addresses.get(address=ip_str, device_id=device_id)
    if not ip_obj:
        ip_obj = nb.ipam.ip_addresses.get(address=ip_str)
    if not ip_obj:
        nb.ipam.ip_addresses.create(
            {
                "address": ip_str,
                "assigned_object_type": "dcim.interface",
                "assigned_object_id": nb_if.id,
                "description": description,
            }
        )
        return True
    update_payload = {}
    if ip_obj.assigned_object_id != nb_if.id or ip_obj.assigned_object_type != "dcim.interface":
        update_payload["assigned_object_type"] = "dcim.interface"
        update_payload["assigned_object_id"] = nb_if.id
    if description and ip_obj.description != description:
        update_payload["description"] = description
    if update_payload:
        ip_obj.update(update_payload)
        return True
    return False


def _ensure_prefix(nb, device, ip_str, seen_prefixes):
    """Garante que o PREFIXO (a rede, ex: '10.0.0.0/24') de 'ip_str'
    esteja cadastrado em IPAM > Prefixes -- o NetBox NÃO cria isso
    sozinho quando um IP Address é criado dentro dele; sem o prefixo, o
    IPAM fica incompleto (só o host solto, sem hierarquia/visão de
    utilização da rede). Ignora endereço /32 (ou /128 em IPv6): não é
    uma sub-rede de verdade, só um host isolado (ex: IP sem máscara
    descoberta, ver collect_snmp/collect_ssh), então criar um "prefixo"
    do tamanho de um host não ajuda em nada.

    'seen_prefixes' é um set compartilhado entre chamadas dentro do
    mesmo apply_device_result() -- evita bater na API de novo pro
    mesmo prefixo quando várias interfaces do device caem na mesma
    sub-rede (comum: várias VLANs/portas na mesma rede de gerência), e
    também serve pra reportar quantos prefixos DISTINTOS (novos ou já
    existentes) participaram desse apply na mensagem de confirmação.

    Retorna True se criou um prefixo novo."""
    try:
        iface_ip = ipaddress.ip_interface((ip_str or "").strip())
    except ValueError:
        return False
    network = iface_ip.network
    if network.prefixlen == network.max_prefixlen:
        return False  # /32 ou /128 -- host isolado, não é sub-rede

    prefix_str = str(network)
    if prefix_str in seen_prefixes:
        return False
    seen_prefixes.add(prefix_str)

    if nb.ipam.prefixes.get(prefix=prefix_str):
        return False

    payload = {"prefix": prefix_str, "status": "active"}
    try:
        if device.site:
            payload["site"] = device.site.id
    except Exception:
        pass
    nb.ipam.prefixes.create(payload)
    return True


def apply_device_result(nb, device, data, interface_filter=None):
    """Aplica um resultado de coleta (dict, mesmo formato salvo em JSON
    pelo CLI) num Device já carregado do pynetbox.

    interface_filter: dict opcional {nome_da_interface: {"include": bool,
    "description": str, "type": str|None, "parent_name": str|None,
    "lag_name": str|None}} -- usado pela interface web pra permitir
    marcar quais interfaces entram, editar a descrição, confirmar o
    tipo (ver guess_interface_type), vincular à porta física
    correspondente pra interfaces de VLAN (campo "parent" do NetBox) e
    vincular a uma LAG (campo "lag" do NetBox, ex: 'lag-1' no Datacom,
    'Eth-Trunk1' no Huawei -- cria a interface tipo LAG automaticamente
    se ainda não existir, ver terceira passada abaixo). Se None, aplica
    todas as interfaces descobertas como vieram, sem tipo nem vínculo
    nenhum (comportamento do CLI).

    Retorna a lista de strings descrevendo o que mudou (changes).
    """
    changes = []

    # Blindagem contra dado antigo em disco (JSON salvo antes desse fix)
    # ou qualquer fonte que não devolva string -- o NetBox rejeita com
    # 400 "Not a valid string" se o serial vier como int/outro tipo.
    serial = data.get("serial")
    if serial is not None and not isinstance(serial, str):
        serial = str(serial)
    if serial:
        serial = serial.strip()
    if serial and serial != device.serial:
        device.update({"serial": serial})
        changes.append(f"serial={serial}")

    existing_if = list(nb.dcim.interfaces.filter(device_id=device.id))
    existing_by_norm = {normalize_ifname(i.name): i for i in existing_if}
    created, updated, matched_norm, ip_written, prefixes_created = 0, 0, 0, 0, 0
    processed_by_name = {}  # nome descoberto -> objeto pynetbox da interface (nova ou já existente)
    pending_parents = []  # [(nome_da_vlan, nome_da_interface_pai)]
    pending_lags = []  # [(nome_da_interface_membro, nome_da_lag)]
    seen_prefixes = set()  # evita checar/criar o mesmo prefixo mais de uma vez neste apply

    for iface in data.get("interfaces", []):
        name = iface.get("name")
        if not name:
            continue

        iface_type = None
        parent_name = None
        lag_name = None
        if interface_filter is not None:
            entry = interface_filter.get(name)
            if not entry or not entry.get("include", True):
                continue
            description = entry.get("description", iface.get("descr") or "")
            iface_type = entry.get("type") or None
            parent_name = entry.get("parent_name") or None
            lag_name = entry.get("lag_name") or None
            # O NetBox só aceita "parent" em interface type=virtual -- se
            # veio vínculo de VLAN da revisão, força o tipo aqui (a API
            # rejeita com 400 "Only virtual interfaces may be assigned to
            # a parent interface" senão), independente do que o dropdown
            # de Tipo tinha selecionado.
            if parent_name:
                iface_type = "virtual"
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
        if lag_name:
            pending_lags.append((name, lag_name))

        # IP(s) descoberto(s) na porta (SNMP ipAddrTable e/ou SSH via
        # TextFSM, ver collect_snmp()/collect_ssh()) -- grava no IPAM e
        # associa a essa interface, com a MESMA descrição da interface
        # (equipamento não expõe uma descrição por IP separada da
        # descrição da porta, ver _sync_interface_ip). Best-effort por
        # IP: um endereço mal formado ou um conflito específico não
        # pode derrubar a aplicação do resto do device.
        for ip_str in iface.get("ip_addresses") or []:
            try:
                if _sync_interface_ip(nb, nb_if, ip_str, device.id, description=description):
                    ip_written += 1
                if _ensure_prefix(nb, device, ip_str, seen_prefixes):
                    prefixes_created += 1
            except Exception:
                pass

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

    # Terceira passada: vincula interfaces físicas membras de uma LAG
    # (ex: ten-gigabit-ethernet-1/1/1 -> lag-1 no Datacom, GigabitEthernet
    # -> Eth-Trunk1 no Huawei) -- cria a interface tipo LAG no device se
    # ainda não existir (find-or-create, cacheado por nome normalizado pra
    # não duplicar quando várias membras apontam pra mesma LAG), depois
    # seta o campo "lag" (FK pra outra Interface do mesmo device) em cada
    # membro. Roda depois da 2ª passada pra não competir com o vínculo de
    # VLAN por engano (LAG e VLAN-parent são campos diferentes do NetBox).
    lag_created = 0
    lag_linked = 0
    lag_cache = {}  # nome normalizado da LAG -> objeto pynetbox da interface LAG
    for member_name, lag_name in pending_lags:
        member_if = processed_by_name.get(member_name)
        if not member_if:
            continue

        norm_lag = normalize_ifname(lag_name)
        lag_if = lag_cache.get(norm_lag)
        if not lag_if:
            lag_if = processed_by_name.get(lag_name) or existing_by_norm.get(norm_lag)
            if not lag_if:
                lag_if = nb.dcim.interfaces.create(
                    {
                        "device": device.id,
                        "name": lag_name,
                        "type": "lag",
                        "enabled": True,
                    }
                )
                lag_created += 1
            lag_cache[norm_lag] = lag_if
            processed_by_name.setdefault(lag_name, lag_if)

        if lag_if.id == member_if.id:
            continue  # não deveria acontecer, mas evita uma interface virar LAG de si mesma

        try:
            current_lag_id = member_if.lag.id if member_if.lag else None
        except AttributeError:
            current_lag_id = None
        if current_lag_id != lag_if.id:
            member_if.update({"lag": lag_if.id})
            lag_linked += 1

    if created:
        changes.append(f"{created} interface(s) criada(s)")
    if updated:
        changes.append(f"{updated} interface(s) atualizada(s)")
    if matched_norm:
        changes.append(f"{matched_norm} casada(s) por nome normalizado")
    if linked:
        changes.append(f"{linked} VLAN(s) vinculada(s) à interface física")
    if ip_written:
        changes.append(f"{ip_written} IP(s) gravado(s)/associado(s) no IPAM")
    if seen_prefixes:
        if prefixes_created:
            changes.append(f"{len(seen_prefixes)} prefixo(s) no IPAM ({prefixes_created} novo(s))")
        else:
            changes.append(f"{len(seen_prefixes)} prefixo(s) confirmado(s) no IPAM")

    return changes


# --------------------------------------------------------------------
# Helpers usados só pela interface web (discovery-ui/) -- cadastro de
# device novo e IP de gerência sem precisar entrar no NetBox direto.
# --------------------------------------------------------------------

MGMT_INTERFACE_NAME = "mgmt-discovery"

# Mensagem que o NetBox devolve quando "Enforce unique space" (global,
# em Settings > IPAM) está ligado e o endereço já existe -- comum em
# clientes que já tinham o IPAM preenchido antes de instalar o Oracle.
# Ex: "Duplicate IP address found in global table: 45.6.176.34/30".
_DUPLICATE_IP_RE = re.compile(r"Duplicate IP address found in \S+ table: (\S+)")


def _extract_duplicate_cidr(exc):
    """Extrai o endereço/prefixo do IP conflitante da mensagem de erro
    de duplicidade do NetBox, pra dar pra buscar e reaproveitar o
    registro que já existe em vez de só repassar o 400 cru pro
    operador (ver set_primary_ip abaixo)."""
    text = str(getattr(exc, "error", "") or "") or str(exc)
    match = _DUPLICATE_IP_RE.search(text)
    return match.group(1) if match else None


def _find_ip_by_cidr(nb, cidr):
    """Localiza um IPAddress já existente cujo HOST bate com 'cidr' (ex:
    '45.6.176.1/28') -- usado depois de um 400 de endereço duplicado
    (ver set_primary_ip), pra reaproveitar o registro em vez de travar.

    Tenta os filtros da API do NetBox em ordem de confiabilidade: o
    filtro 'address' (exato) deveria bastar sozinho, mas não confiamos
    só nele -- em algum ambiente/versão ele pode não bater por string
    exata (ex: normalização de zero à esquerda, ordem dos octetos em
    IPv6 etc.), o que faria a gente devolver None e repassar o 400 cru
    de novo mesmo com o registro existindo de verdade. Cai pro filtro
    'parent' (a rede que contém o host -- filtro padrão e bem estável
    do NetBox) e por fim uma busca textual (q=) antes de desistir.
    Sempre confere o HOST exato (ignora o prefixo) no resultado antes de
    aceitar, pra não pegar um vizinho por engano numa busca mais larga
    (parent/q podem devolver vários hosts da mesma rede)."""
    try:
        host = str(ipaddress.ip_interface(cidr).ip)
    except ValueError:
        host = cidr.split("/")[0]

    def _first_host_match(candidates):
        for c in candidates:
            if str(c.address).split("/")[0] == host:
                return c
        return None

    found = _first_host_match(nb.ipam.ip_addresses.filter(address=cidr))
    if found:
        return found

    try:
        network = str(ipaddress.ip_interface(cidr).network)
        found = _first_host_match(nb.ipam.ip_addresses.filter(parent=network))
        if found:
            return found
    except ValueError:
        pass

    return _first_host_match(nb.ipam.ip_addresses.filter(q=host))


def _clear_stale_primary_ip_refs(nb, ip_obj):
    """Se 'ip_obj' está marcado como primary_ip4 (ou primary_ip6) de
    QUALQUER device -- muito comum em cliente com IPAM pré-existente: o
    IP já estava associado a uma interface física de verdade e marcado
    como Primary daquele device antes do Oracle existir -- o NetBox
    recusa reassociar esse registro a outra interface com 400 "Cannot
    reassign IP address while it is designated as the primary IP for
    the parent object". Localiza o(s) device(s) dono(s) via filtro
    primary_ip4_id/primary_ip6_id (não dá pra confiar só no
    assigned_object atual do IP pra achar isso) e limpa a referência
    antes da reassociação -- quem chama (set_primary_ip) recria a
    referência certa pro device atual logo em seguida. Best-effort: se
    o filtro falhar por algum motivo, não trava a chamada -- o
    ip_obj.update() seguinte vai devolver o 400 real do NetBox se ainda
    houver algum bloqueio."""
    for field in ("primary_ip4", "primary_ip6"):
        try:
            owners = list(nb.dcim.devices.filter(**{f"{field}_id": ip_obj.id}))
        except Exception:
            owners = []
        for owner in owners:
            try:
                owner.update({field: None})
            except Exception:
                pass


def set_primary_ip(nb, device, ip_str):
    """Garante que 'device' tenha 'ip_str' (ex: '10.0.0.1' ou
    '10.0.0.1/32') como Primary IPv4. No modelo do NetBox, um IP só
    pode virar Primary IPv4 se já estiver associado a uma Interface DO
    PRÓPRIO device -- por isso criamos (ou reaproveitamos) uma interface
    dedicada MGMT_INTERFACE_NAME pra isso, em vez de exigir que o
    operador entenda esse detalhe do modelo de dados.

    Clientes que já têm o IPAM preenchido antes de instalar o Oracle
    costumam ter esse host já cadastrado de algum jeito -- às vezes com
    outro prefixo (ex: '45.6.176.34/30', parte de um bloco documentado
    antes: nosso lookup abaixo só acha por string EXATA (endereço+
    prefixo), então não bate com a tentativa em '/32' e a criação
    esbarra na validação "Enforce unique space" do NetBox), às vezes já
    associado a uma interface de OUTRO device (ex: cadastrado na mão
    antes de existir vínculo de descoberta -- NetBox recusa com "The
    specified IP address is not assigned to this device" se a gente só
    tentar apontar primary_ip4 pra ele sem mover a associação), às vezes
    já marcado como Primary IPv4/6 de algum device (NetBox recusa com
    "Cannot reassign IP address while it is designated as the primary
    IP for the parent object" se a gente só tentar mudar a interface
    dele sem limpar essa referência primeiro).

    Em vez de estourar esse 400 cru pro operador, tratamos o que foi
    digitado na tela como fonte de verdade de onde esse IP está de
    verdade hoje -- mesmo padrão já usado em _sync_interface_ip() pra
    IP de interface descoberta automaticamente -- e REASSOCIAMOS o
    registro existente (de qualquer device/interface que estiver) pra
    cá, em vez de bloquear pedindo ajuste manual no NetBox.
    """
    ip_str = ip_str.strip()
    if "/" not in ip_str:
        ip_str = f"{ip_str}/32"

    # Valida ANTES de mandar pro NetBox -- um endereço mal formado (ex:
    # '456.176.1', faltando um ponto) não é rejeitado de forma limpa por
    # todo endpoint/versão do NetBox: já vimos isso virar um 500 cru
    # "Internal Server Error" (com traceback tipo KeyError) em vez de um
    # 400 de validação normal, porque o parsing de endereço do lado do
    # NetBox nem sempre trata string totalmente inválida com uma exceção
    # tratada. Falhar aqui, do nosso lado, com uma mensagem clara em
    # português, evita expor esse erro cru pro operador.
    try:
        ipaddress.ip_interface(ip_str)
    except ValueError:
        raise RuntimeError(
            f"'{ip_str.split('/')[0]}' não é um endereço IP válido -- confira "
            f"se não falta ou sobra algum ponto/dígito."
        )

    iface = nb.dcim.interfaces.get(device_id=device.id, name=MGMT_INTERFACE_NAME)
    if not iface:
        iface = nb.dcim.interfaces.create(
            {"device": device.id, "name": MGMT_INTERFACE_NAME, "type": "virtual"}
        )

    ip_obj = nb.ipam.ip_addresses.get(address=ip_str, device_id=device.id)
    if not ip_obj:
        # Pode já existir um IP com esse endereço em outro objeto (ex:
        # criado antes sem interface associada, ou associado a outro
        # device) -- tenta achar por endereço puro antes de criar um
        # novo, pra não duplicar.
        ip_obj = nb.ipam.ip_addresses.get(address=ip_str)
    if not ip_obj:
        try:
            ip_obj = nb.ipam.ip_addresses.create(
                {"address": ip_str, "assigned_object_type": "dcim.interface", "assigned_object_id": iface.id}
            )
        except pynetbox.RequestError as exc:
            # Endereço já existe com outro prefixo (ver docstring) --
            # extrai o CIDR real da mensagem de erro do NetBox e busca
            # o registro existente pra reaproveitar (ver _find_ip_by_cidr,
            # tenta vários filtros -- não confia só no 'address' exato).
            existing_cidr = _extract_duplicate_cidr(exc)
            if not existing_cidr:
                raise
            ip_obj = _find_ip_by_cidr(nb, existing_cidr)
            if not ip_obj:
                raise

    # Reassocia o registro (de qualquer device/interface que estiver,
    # ou nenhum) pra nossa interface dedicada -- é o que faz esse IP
    # poder virar Primary IPv4 DESTE device.
    if ip_obj.assigned_object_id != iface.id or ip_obj.assigned_object_type != "dcim.interface":
        # Se esse IP já é o primary_ip4/6 de ALGUM device (muito comum
        # em cliente com IPAM pré-existente: o IP já estava associado a
        # uma interface física de verdade e marcado como Primary IPv4
        # daquele mesmo device antes do Oracle existir), o NetBox recusa
        # a reassociação com 400 "Cannot reassign IP address while it is
        # designated as the primary IP for the parent object" -- precisa
        # limpar essa referência do lado do device dono primeiro. Não
        # precisa se preocupar em perder essa referência: se o dono for
        # o PRÓPRIO device, o device.update({"primary_ip4": ...}) logo
        # abaixo recria ela certinha; se for outro device, é exatamente
        # a reassociação que o operador pediu (ver docstring).
        _clear_stale_primary_ip_refs(nb, ip_obj)
        ip_obj.update({"assigned_object_type": "dcim.interface", "assigned_object_id": iface.id})

    device.update({"primary_ip4": ip_obj.id})
    return ip_obj


def set_discovery_fields(device, method, discovery_username=None, discovery_password=None, discovery_snmp_community=None, discovery_ssh_port=None):
    """Grava discovery_method + as credenciais recebidas nos custom
    fields do device (mesmos campos criados por create_discovery_fields.py).

    Cada campo é gravado sempre que vier preenchido (não None),
    INDEPENDENTE do method calculado -- method é só um resumo derivado
    de quais credenciais já estão completas (ver
    app.py:_apply_discovery_form), não um portão de quais campos podem
    ser salvos. ANTES essa gravação era condicionada a
    method in ("ssh", "snmp", "both"): preencher só o usuário (sem
    senha ainda -- ex: host novo, editando campo por campo) fazia
    method sair None (SSH só conta como "completo" com usuário E senha
    juntos) e o usuário digitado nunca era gravado, mesmo a requisição
    dando certo -- a linha recarregava sem o que foi digitado, parecendo
    um refresh que "comeu" o campo. Preencher a community primeiro
    "destravava" porque sozinha já fecha method="snmp", que então
    liberava a gravação de tudo, inclusive o usuário pendente.

    discovery_password é cifrado (ver encrypt_secret) antes de gravar --
    o NetBox guarda só o texto cifrado no custom field, nunca a senha em
    claro. decrypt_secret() é chamado do outro lado, no único lugar que
    de fato usa a senha pra conectar (collect_device())."""
    cf = dict(device.custom_fields or {})
    cf["discovery_method"] = method
    if discovery_username is not None:
        cf["discovery_username"] = discovery_username
    if discovery_password is not None:
        cf["discovery_password"] = encrypt_secret(discovery_password)
    if discovery_ssh_port is not None:
        cf["discovery_ssh_port"] = discovery_ssh_port
    if discovery_snmp_community is not None:
        cf["discovery_snmp_community"] = discovery_snmp_community
    device.update({"custom_fields": cf})
    return device


def set_connectivity_status(device, ssh_status=None, ssh_status_detail=None, snmp_status=None, snmp_status_detail=None):
    """Grava o resultado da checagem rápida de conectividade (ver
    test_ssh_connectivity()/test_snmp_connectivity() abaixo) nos custom
    fields do device -- é isso que colore os badges SSH/SNMP estilo
    Zabbix no dashboard (ver app.py:_build_row/_device_row.html).
    status vale "ok"/"error"/"" ("" limpa de volta pro cinza -- usado
    quando o operador apaga a credencial daquele protocolo, ver
    app.py:_apply_discovery_form()). Só grava o par status/detail que
    vier com status != None, pra dar pra atualizar só um dos dois
    protocolos sem mexer no outro."""
    cf = dict(device.custom_fields or {})
    if ssh_status is not None:
        cf["discovery_ssh_status"] = ssh_status
        cf["discovery_ssh_status_detail"] = ssh_status_detail or ""
    if snmp_status is not None:
        cf["discovery_snmp_status"] = snmp_status
        cf["discovery_snmp_status_detail"] = snmp_status_detail or ""
    device.update({"custom_fields": cf})
    return device


def test_ssh_connectivity(device, username, password, port=None, timeout=6):
    """Checagem RÁPIDA e real de conectividade SSH: só abre a sessão e
    autentica, não roda nenhum comando -- pensada pro indicador verde/
    vermelho do dashboard (estilo Zabbix), disparada pela edição inline
    sempre que usuário/senha/porta/IP mudam (ver
    app.py:_apply_discovery_form()), então precisa ser rápida.

    Usa device_type="generic_ssh" DE PROPÓSITO em vez de resolver o
    driver certo do fabricante (ver resolve_ssh_device_type(), usado
    pela coleta de verdade em collect_ssh()) -- aqui só queremos saber
    se dá pra autenticar, e o passo de "session_preparation" de um
    driver específico (desabilitar paginação, achar o prompt certo)
    pode falhar por um motivo que não tem nada a ver com a credencial
    estar certa ou o device estar alcançável (o que geraria um
    vermelho enganoso). generic_ssh é mais tolerante nesse passo --
    mesma escolha já usada em _collect_datacom() pra fabricante sem
    driver Netmiko dedicado.

    Retorna (True, None) se autenticou, (False, "motivo curto") se não.
    """
    if not device.primary_ip4:
        return False, "sem IP de gerência"
    host = str(device.primary_ip4).split("/")[0]
    if not username or not password:
        return False, "usuário/senha não preenchidos"

    from netmiko import ConnectHandler, NetmikoAuthenticationException, NetmikoTimeoutException

    try:
        conn = ConnectHandler(
            device_type="generic_ssh",
            host=host,
            username=username,
            password=password,
            port=int(port) if port else 22,
            timeout=timeout,
        )
    except NetmikoAuthenticationException:
        return False, "usuário/senha rejeitados pelo device"
    except NetmikoTimeoutException:
        return False, "timeout (IP/porta inacessível ou SSH desligado)"
    except Exception as exc:
        return False, str(exc)[:200]

    try:
        conn.disconnect()
    except Exception:
        pass
    return True, None


def test_snmp_connectivity(device, community, timeout=3, retries=0):
    """Checagem RÁPIDA de conectividade SNMP: um snmpget só (sysName),
    sem walk nenhum -- mesma ideia/uso do test_ssh_connectivity()
    acima, pro indicador SNMP. Timeout/retries bem menores que os
    usados na coleta de verdade (collect_snmp) porque isso roda a cada
    edição inline da community, não pode travar a tela por muito
    tempo."""
    if not device.primary_ip4:
        return False, "sem IP de gerência"
    host = str(device.primary_ip4).split("/")[0]
    if not community:
        return False, "community não preenchida"
    try:
        value = snmp_get(host, community, OID_SYS_NAME, timeout=timeout, retries=retries)
    except Exception as exc:
        return False, str(exc)[:200]
    if value:
        return True, None
    return False, "sem resposta (community errada ou SNMP desligado)"


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
