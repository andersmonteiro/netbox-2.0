#!/usr/bin/env python3
"""
discovery-ui / app.py
======================
Interface web pra quem não usa terminal (ex: time comercial) revisar e
aprovar descobertas de rede antes de gravar no NetBox. Mesma lógica do
CLI (discovery_netbox.py), reaproveitada via discovery_core.py -- os
dois front-ends compartilham a pasta discovery_output/ como "caixa de
entrada" de pendências, então um device coletado pelo CLI também
aparece pra revisão aqui, e vice-versa.

Fluxo:
  /            -> dashboard: lista devices do NetBox, seleciona quais
                  rodar a descoberta
  /device/new  -> cadastra um device novo (Site/Role/Type + IP +
                  método + credencial de descoberta)
  /device/<id>/edit -> edita IP/Platform/credencial de um device já
                  existente
  /discover (POST) -> roda a coleta nos devices selecionados, grava em
                  discovery_output/*.json (nada é gravado no NetBox
                  ainda)
  /review      -> mostra o que foi coletado, com checkbox por
                  interface (inclui/exclui) e campo de descrição
                  editável -- só grava no NetBox quando o operador
                  aprova explicitamente

Variáveis de ambiente: NETBOX_URL, NETBOX_TOKEN, DISCOVERY_UI_USER,
DISCOVERY_UI_PASSWORD, FLASK_SECRET_KEY, SNMP_TIMEOUT, SNMP_RETRIES
"""

import json
import os
import sys
import threading
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import docker
from docker.errors import DockerException, NotFound
from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for
from markupsafe import Markup, escape
from werkzeug.security import check_password_hash, generate_password_hash

sys.path.insert(0, str(Path(__file__).parent))
import discovery_core as core

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "troque-esta-chave-insegura")

NETBOX_URL = os.environ.get("NETBOX_URL")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")
UI_USER = os.environ.get("DISCOVERY_UI_USER", "admin")
UI_PASSWORD = os.environ.get("DISCOVERY_UI_PASSWORD", "")
SNMP_TIMEOUT = int(os.environ.get("SNMP_TIMEOUT", "5"))
SNMP_RETRIES = int(os.environ.get("SNMP_RETRIES", "1"))
# Filtro de nome usado pela aba "Serviços" (status/reinício de containers,
# ver _list_service_containers()/_get_docker_client() mais abaixo) --
# lista separada por vírgula; entra qualquer container cujo nome CONTÉM
# ao menos UM desses termos (case-insensitive). Postgres/Redis não têm
# "netbox" no nome (container_name explícito no compose, ou nome dado
# por outro projeto Compose que sobe o NetBox em si -- ver
# _SERVICE_LABEL_RULES logo abaixo), por isso entram como termos
# separados. Vazio ("") mostraria TODOS os containers do host.
SERVICE_NAME_FILTER = os.environ.get("ORACLE_SERVICE_FILTER", "netbox,redis,postgres")

OUTPUT_DIR = Path(__file__).parent / "discovery_output"
APPLIED_DIR = OUTPUT_DIR / "applied"
DISCARDED_DIR = OUTPUT_DIR / "discarded"

# --------------------------------------------------------------------
# Configuração da URL/token do NetBox: por padrão vem das variáveis de
# ambiente (preenchidas na instalação), mas pode ser trocada depois pela
# tela de Configurações (/settings) sem precisar mexer no .env nem
# reiniciar container -- útil porque o netbox-docker tem um bug
# conhecido (netbox-community/netbox-docker#1647) onde o token real
# gerado pelo NetBox às vezes não bate com o configurado. Guardamos
# dentro de uma SUBPASTA de discovery_output/ (que já é um bind mount
# persistente) -- não pode ficar solto direto em discovery_output/,
# porque dashboard() e review() fazem glob("*.json") ali pra contar/
# listar descobertas pendentes, e glob não-recursivo ignora subpastas,
# então isolar aqui evita o arquivo de config ser contado como se fosse
# uma descoberta aguardando revisão.
# --------------------------------------------------------------------
SETTINGS_PATH = OUTPUT_DIR / "_oracle" / "settings.json"


def load_settings():
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            return {
                "netbox_url": data.get("netbox_url") or NETBOX_URL or "",
                "netbox_token": data.get("netbox_token") or NETBOX_TOKEN or "",
                "ui_password_hash": data.get("ui_password_hash") or "",
            }
        except Exception:
            pass
    return {"netbox_url": NETBOX_URL or "", "netbox_token": NETBOX_TOKEN or "", "ui_password_hash": ""}


def save_settings(**fields):
    """Faz merge de 'fields' em cima do settings.json existente (em vez
    de sobrescrever o arquivo inteiro) -- assim salvar a config da API
    não apaga a senha trocada (ui_password_hash) e vice-versa, já que os
    dois usam o mesmo arquivo."""
    current = {}
    if SETTINGS_PATH.exists():
        try:
            current = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            current = {}
    current.update({k: v for k, v in fields.items() if v is not None})
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")


def check_ui_password(password):
    """Confere a senha de login. Se o operador já trocou a senha pela
    tela 'Alterar senha', usa o hash salvo em settings.json; senão cai
    pro DISCOVERY_UI_PASSWORD do .env (senha padrão definida na
    instalação) -- assim a troca de senha funciona sem precisar mexer
    no .env nem reiniciar o container, mesmo padrão já usado pra URL/
    token do NetBox (ver load_settings/save_settings acima)."""
    stored_hash = load_settings().get("ui_password_hash")
    if stored_hash:
        return check_password_hash(stored_hash, password)
    return bool(password) and bool(UI_PASSWORD) and password == UI_PASSWORD


def get_nb():
    settings = load_settings()
    url, token = settings["netbox_url"], settings["netbox_token"]
    if not url or not token:
        raise RuntimeError("NetBox ainda não configurado -- acesse Configurações da API.")
    return core.get_client(url, token)


def _display_case(value):
    """Ajeita a exibição de nomes vindos do NetBox (Site, Manufacturer,
    Device Type, Platform...): quando o valor está TODO em minúsculo
    (ex: 'mikrotik', 'santarem'), aplica Title Case só pra leitura na
    tela. Não mexe em nada que já tenha alguma letra maiúscula (evita
    estragar siglas/nomes de marca digitados certo, tipo 'MikroTik',
    'ZTE', 'IOS-XE') -- e não muda o valor usado pra salvar, só o texto
    mostrado."""
    if not value:
        return value
    return value.title() if value == value.lower() else value


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# Rotas que não dependem do NetBox estar configurado (senão a gente
# criaria um loop de redirect pra /settings).
_NETBOX_INDEPENDENT_ENDPOINTS = {
    "login", "logout", "settings_view", "api_status", "change_password", "static",
    "services_status", "service_restart",
}


@app.before_request
def require_netbox_configured():
    if not session.get("logged_in"):
        return
    if request.endpoint in _NETBOX_INDEPENDENT_ENDPOINTS:
        return
    settings = load_settings()
    if not settings.get("netbox_url") or not settings.get("netbox_token"):
        flash("Configure a URL e o token do NetBox antes de continuar.", "error")
        return redirect(url_for("settings_view"))


# --------------------------------------------------------------------
# login / logout
# --------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == UI_USER and password and check_ui_password(password):
            session["logged_in"] = True
            session["username"] = username
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Usuário ou senha incorretos.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------
# configurações da API do NetBox (URL + token) e status ao vivo
# --------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_view():
    current = load_settings()
    if request.method == "POST":
        url = request.form.get("netbox_url", "").strip()
        token = request.form.get("netbox_token", "").strip()
        if not url:
            flash("Informe a URL do NetBox.", "error")
            return redirect(url_for("settings_view"))
        if not token:
            token = current.get("netbox_token", "")
        save_settings(netbox_url=url, netbox_token=token)
        flash("Configuração da API salva.", "success")
        return redirect(url_for("settings_view"))

    masked_token = ""
    existing_token = current.get("netbox_token", "")
    if existing_token:
        masked_token = f"{existing_token[:4]}…{existing_token[-4:]}" if len(existing_token) > 8 else "••••"

    return render_template(
        "settings.html",
        netbox_url=current.get("netbox_url", ""),
        masked_token=masked_token,
    )


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not check_ui_password(current_password):
            flash("Senha atual incorreta.", "error")
        elif len(new_password) < 6:
            flash("A nova senha precisa ter pelo menos 6 caracteres.", "error")
        elif new_password != confirm_password:
            flash("A confirmação não bate com a nova senha.", "error")
        else:
            save_settings(ui_password_hash=generate_password_hash(new_password))
            flash("Senha alterada com sucesso.", "success")
            return redirect(url_for("change_password"))
    return render_template("change_password.html")


@app.route("/api/status")
@login_required
def api_status():
    settings = load_settings()
    url, token = settings.get("netbox_url"), settings.get("netbox_token")
    if not url or not token:
        return {"status": "unconfigured", "detail": "NetBox ainda não configurado"}

    try:
        nb = core.get_client(url, token)
        nb.dcim.sites.count()
        return {"status": "online", "detail": url}
    except Exception as exc:
        msg = str(exc)
        low = msg.lower()
        if "403" in msg or "forbidden" in low or "token" in low or "401" in msg:
            return {"status": "unauthorized", "detail": "Token inválido ou sem permissão"}
        return {"status": "offline", "detail": msg[:200]}


# --------------------------------------------------------------------
# status dos serviços (containers Docker do host) + reinício
# --------------------------------------------------------------------
# Igual em espírito ao status da API acima (badge com bolinha verde/
# vermelha no navbar) -- só que em vez de testar a API do NetBox, lista
# os containers do host via socket do Docker montado em
# /var/run/docker.sock (ver volume em docker-compose.*.yml). Client é
# lazy + cacheado num módulo global: se o socket não estiver montado
# (ex: quem não quis expor essa permissão, ver comentário no compose),
# a conexão falha UMA vez, guardamos o motivo, e toda chamada seguinte
# devolve "indisponível" direto sem tentar de novo a cada request.
_docker_client = None
_docker_unavailable_reason = None


def _get_docker_client():
    global _docker_client, _docker_unavailable_reason
    if _docker_client is not None:
        return _docker_client
    if _docker_unavailable_reason is not None:
        return None
    try:
        client = docker.DockerClient(base_url="unix://var/run/docker.sock")
        client.ping()
    except DockerException as exc:
        _docker_unavailable_reason = str(exc)
        return None
    except Exception as exc:
        _docker_unavailable_reason = str(exc)
        return None
    _docker_client = client
    return client


def _service_filter_terms():
    return [t.strip().lower() for t in SERVICE_NAME_FILTER.split(",") if t.strip()]


def _in_service_scope(name):
    terms = _service_filter_terms()
    low = name.lower()
    return (not terms) or any(t in low for t in terms)


# Nome "cru" do container (o que o Docker/Compose deu -- geralmente
# "<projeto>-<serviço>-<N>", ex: "natverk-docker-netbox-housekeeping-1",
# ou o container_name explícito tipo "netbox-oracle"/"postgres") pra um
# rótulo curto e consistente na UI, independente de qual projeto Compose
# subiu o NetBox (o nome do projeto varia por instalação -- "natverk-
# docker", "netbox-docker", o que o operador chamou a pasta). Ordem
# importa: checa os termos mais específicos ("netbox-housekeeping")
# antes dos genéricos ("netbox"), senão "netbox" bate primeiro e
# "netbox-housekeeping"/"netbox-worker"/"netbox-oracle" nunca são
# alcançados. Container que não bate com nenhuma regra mantém o nome
# cru (fallback -- não esconde nada, só não fica "bonito").
_SERVICE_LABEL_RULES = (
    ("netbox-housekeeping", "netbox-housekeeping"),
    ("netbox-worker", "netbox-worker"),
    ("netbox-oracle", "netbox-oracle"),
    ("redis-cache", "netbox-redis-cache"),
    ("postgres", "netbox-postgres"),
    ("redis", "netbox-redis"),
    ("netbox", "netbox"),
)


def _service_label(raw_name):
    low = raw_name.lower()
    for term, label in _SERVICE_LABEL_RULES:
        if term in low:
            return label
    return raw_name


def _list_service_containers():
    client = _get_docker_client()
    if client is None:
        return None
    containers = []
    for c in client.containers.list(all=True):
        if not _in_service_scope(c.name):
            continue
        containers.append({"name": c.name, "label": _service_label(c.name), "status": c.status})
    containers.sort(key=lambda x: x["label"])
    return containers


@app.route("/api/services/status")
@login_required
def services_status():
    containers = _list_service_containers()
    if containers is None:
        return {"available": False, "error": _docker_unavailable_reason or "Docker indisponível"}
    running = sum(1 for c in containers if c["status"] == "running")
    return {"available": True, "containers": containers, "total": len(containers), "running": running}


@app.route("/api/services/<path:name>/restart", methods=["POST"])
@login_required
def service_restart(name):
    client = _get_docker_client()
    if client is None:
        return {"ok": False, "error": _docker_unavailable_reason or "Docker indisponível"}, 503
    # Reforço além do filtro por nome já aplicado na listagem -- mesmo
    # que alguém chame esse endpoint direto (fora do dropdown), só deixa
    # reiniciar container dentro do escopo do NetBox, nunca qualquer
    # container do host (o socket já dá acesso total ao Docker, mas essa
    # rota especificamente fica restrita ao que a própria UI mostra).
    if not _in_service_scope(name):
        return {"ok": False, "error": "Container fora do escopo permitido."}, 403
    try:
        container = client.containers.get(name)
        container.restart(timeout=10)
        return {"ok": True}
    except NotFound:
        return {"ok": False, "error": "Container não encontrado."}, 404
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}, 500


# --------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------

def _load_sites(nb):
    try:
        sites_raw = list(nb.dcim.sites.all())
    except Exception:
        sites_raw = []
    sites = []
    for s in sites_raw:
        s_region = ""
        try:
            if getattr(s, "region", None):
                s_region = _display_case(str(s.region))
        except Exception:
            s_region = ""
        sites.append({"id": s.id, "name": _display_case(s.name), "region": s_region})
    sites.sort(key=lambda x: x["name"].lower())
    return sites


def _load_device_types(nb):
    try:
        device_types_raw = list(nb.dcim.device_types.all())
    except Exception:
        device_types_raw = []
    device_types = []
    for dt in device_types_raw:
        manuf = ""
        try:
            if getattr(dt, "manufacturer", None):
                manuf = _display_case(str(dt.manufacturer))
        except Exception:
            manuf = ""
        model = _display_case(str(dt.model))
        label = f"{manuf} — {model}" if manuf else model
        device_types.append({"id": dt.id, "label": label, "manufacturer": manuf})
    device_types.sort(key=lambda x: x["label"].lower())
    return device_types


def _build_row(d):
    """Monta o dict usado pela linha do device no dashboard (e reaproveitado
    pelo endpoint /device/<id>/row, que devolve o HTML de UMA linha só pra
    atualizar em vez de recarregar a página inteira -- ver comentário em
    device_row())."""
    cf = d.custom_fields or {}
    method = cf.get("discovery_method")
    has_ssh_cred_pair = bool(cf.get("discovery_username") and cf.get("discovery_password"))
    has_community = bool(cf.get("discovery_snmp_community"))
    if method == "ssh":
        has_cred = has_ssh_cred_pair
    elif method == "snmp":
        has_cred = has_community
    elif method == "both":
        # "both" precisa das DUAS credenciais -- SSH (Netmiko) e SNMP
        # rodam juntos e os resultados são cruzados por interface (ver
        # discovery_core.collect_both()).
        has_cred = has_ssh_cred_pair and has_community
    else:
        has_cred = False
    # Independente do método -- usado só pra mostrar "definida"/"—" na
    # célula de Senha (SSH), que agora também serve pro extra opcional
    # de MikroTik em devices configurados como SNMP. Não entra no
    # cálculo de "ready" isolado: a checagem extra do MikroTik é
    # opcional, não bloqueia a descoberta principal -- mas pra
    # method="both" ela É obrigatória (embutida no has_cred acima).
    has_ssh_cred = has_ssh_cred_pair
    site = _display_case(str(d.site)) if d.site else ""
    region = ""
    try:
        if d.site and getattr(d.site, "region", None):
            region = _display_case(str(d.site.region))
    except Exception:
        region = ""
    manufacturer = ""
    device_type_model = ""
    try:
        if d.device_type:
            device_type_model = _display_case(str(d.device_type))
            if getattr(d.device_type, "manufacturer", None):
                manufacturer = _display_case(str(d.device_type.manufacturer))
    except Exception:
        pass
    # Platform (override manual do device_type) não é mais um campo que o
    # operador precisa escolher no dashboard -- é resolvido automaticamente
    # a partir do Fabricante (ver discovery_core.resolve_ssh_device_type(),
    # mesma regra usada em collect_ssh()). Se o device já tiver uma
    # Platform explícita no NetBox, ela continua valendo (permite override
    # manual); senão, só conta como "pronto" pra SSH/both se o Fabricante
    # for reconhecido.
    platform_resolved = bool(d.platform) or bool(core.resolve_ssh_device_type(manufacturer, device_type_model))

    # Motivo específico de "incompleto" -- sem isso, o operador só via o
    # badge vermelho genérico e a caixa de seleção desabilitada, sem
    # entender o porquê (ex: escolheu method="both" num fabricante tipo
    # MikroTik, que não tem device_type SSH reconhecido aqui -- ver
    # _MANUFACTURER_SSH_RULES em discovery_core.py -- então "both"
    # NUNCA fica pronto pra esse fabricante, mesmo com usuário/senha/
    # community preenchidos). Usado no title da checkbox e do badge de
    # status (ver _device_row.html).
    not_ready_reason = None
    if not method:
        not_ready_reason = "Configure o método de descoberta (SSH e/ou SNMP)."
    elif not d.primary_ip4:
        not_ready_reason = "Device sem IP de gerência (Primary IPv4) no NetBox."
    elif method in ("ssh", "both") and not platform_resolved:
        not_ready_reason = (
            "Esse fabricante não tem um jeito de conectar via SSH reconhecido "
            "aqui (ex: MikroTik e a maioria das OLTs não têm) -- \"SSH + SNMP\" "
            "nunca vai ficar pronto pra esse device. Use só SNMP, ou defina "
            "a Platform manualmente no NetBox com o device_type certo do Netmiko."
        )
    elif not has_cred:
        if method == "both":
            missing = []
            if not has_ssh_cred_pair:
                missing.append("usuário/senha (SSH)")
            if not has_community:
                missing.append("community (SNMP)")
            not_ready_reason = f"Falta {' e '.join(missing)}."
        elif method == "ssh":
            not_ready_reason = "Falta usuário/senha (SSH)."
        elif method == "snmp":
            not_ready_reason = "Falta a community (SNMP)."

    # Cor do badge SSH e SNMP mostrados direto na coluna Status (que
    # absorveu a antiga coluna Conectividade -- ver _device_row.html).
    # O badge em si continua só "SSH"/"SNMP" (igual sempre foi); o
    # motivo de estar vermelho/verde some no title (tooltip, aparece só
    # ao passar o mouse) -- ver conversa que definiu essas mensagens
    # exatas. Prioridade pra decidir a cor e a mensagem:
    # 1) credencial incompleta (diz especificamente o que falta, sem
    #    nem tentar testar -- username primeiro, senha depois) --
    # 2) credencial completa e testada (ok/erro, ver
    #    test_ssh_connectivity()/test_snmp_connectivity() em
    #    discovery_core.py, disparado em _apply_discovery_form()) --
    # 3) fallback neutro (credencial completa mas ainda sem teste
    #    registrado -- não deveria acontecer na prática, já que o teste
    #    roda sozinho assim que os dois campos ficam completos, mas
    #    cobre o caso de dado criado por fora desse app, ex: script/CLI).
    if not cf.get("discovery_username"):
        ssh_badge_cls, ssh_reason, ssh_detail = "bg-red-lt", "Sem usuário informado.", ""
    elif not has_ssh_cred_pair:
        ssh_badge_cls, ssh_reason, ssh_detail = "bg-red-lt", "Sem senha informada.", ""
    elif cf.get("discovery_ssh_status") == "ok":
        ssh_badge_cls, ssh_reason, ssh_detail = "bg-success-lt", "SSH OK.", ""
    elif cf.get("discovery_ssh_status") == "error":
        ssh_badge_cls, ssh_reason = "bg-red-lt", "Usuário/Senha inválidos."
        ssh_detail = cf.get("discovery_ssh_status_detail") or ""
    else:
        ssh_badge_cls, ssh_reason, ssh_detail = "bg-secondary-lt", "Aguardando teste.", ""
    ssh_badge_text = "SSH"
    ssh_badge_tooltip = f"{ssh_reason} ({ssh_detail})" if ssh_detail else ssh_reason

    if not cf.get("discovery_snmp_community"):
        snmp_badge_cls, snmp_reason, snmp_detail = "bg-red-lt", "Sem community", ""
    elif cf.get("discovery_snmp_status") == "ok":
        snmp_badge_cls, snmp_reason, snmp_detail = "bg-success-lt", "SNMP OK.", ""
    elif cf.get("discovery_snmp_status") == "error":
        snmp_badge_cls, snmp_reason = "bg-red-lt", "Sem resposta SNMP."
        snmp_detail = cf.get("discovery_snmp_status_detail") or ""
    else:
        snmp_badge_cls, snmp_reason, snmp_detail = "bg-secondary-lt", "Aguardando teste.", ""
    snmp_badge_text = "SNMP"
    snmp_badge_tooltip = f"{snmp_reason} ({snmp_detail})" if snmp_detail else snmp_reason

    return {
        "id": d.id,
        "name": d.name,
        "site": site,
        "site_id": d.site.id if d.site else None,
        "region": region,
        "manufacturer": manufacturer,
        "device_type_model": device_type_model,
        "device_type_id": d.device_type.id if d.device_type else None,
        "primary_ip": str(d.primary_ip4).split("/")[0] if d.primary_ip4 else None,
        "platform": _display_case(str(d.platform)) if d.platform else None,
        "platform_id": d.platform.id if d.platform else None,
        "method": method,
        "has_cred": has_cred,
        "has_ssh_cred": has_ssh_cred,
        # Resultado da checagem RÁPIDA e real de conectividade (ver
        # test_ssh_connectivity()/test_snmp_connectivity() em
        # discovery_core.py, disparada por app.py:_apply_discovery_form()
        # sempre que a credencial daquele protocolo muda) -- é isso que
        # colore os badges SSH/SNMP estilo Zabbix no dashboard, SEMPRE
        # visíveis (ver _device_row.html): "ok" = verde, "error" =
        # vermelho, "" (nunca testado / sem credencial) = cinza.
        "ssh_status": cf.get("discovery_ssh_status") or "",
        "ssh_status_detail": cf.get("discovery_ssh_status_detail") or "",
        "snmp_status": cf.get("discovery_snmp_status") or "",
        "snmp_status_detail": cf.get("discovery_snmp_status_detail") or "",
        "ssh_badge_cls": ssh_badge_cls,
        "ssh_badge_text": ssh_badge_text,
        "ssh_badge_tooltip": ssh_badge_tooltip,
        "snmp_badge_cls": snmp_badge_cls,
        "snmp_badge_text": snmp_badge_text,
        "snmp_badge_tooltip": snmp_badge_tooltip,
        # Platform (override opcional de device_type) só é obrigatório
        # quando o método envolve SSH ("ssh" ou "both") -- SNMP puro não precisa.
        "ready": bool(method and has_cred and d.primary_ip4 and (method not in ("ssh", "both") or platform_resolved)),
        "not_ready_reason": not_ready_reason,
        "platform_resolved": platform_resolved,
        "cf_username": cf.get("discovery_username") or "",
        "cf_ssh_port": cf.get("discovery_ssh_port") or "",
        "cf_snmp_community": cf.get("discovery_snmp_community") or "",
    }


@app.route("/")
@login_required
def dashboard():
    try:
        nb = get_nb()
        devices = sorted(nb.dcim.devices.all(), key=lambda d: d.name or "")
    except Exception as exc:
        flash(f"Erro ao consultar o NetBox: {exc}", "error")
        devices = []
        nb = None

    try:
        platforms = list(nb.dcim.platforms.all()) if nb else []
    except Exception:
        platforms = []

    sites = _load_sites(nb) if nb else []
    device_types = _load_device_types(nb) if nb else []

    rows = [_build_row(d) for d in devices]

    pending_count = len(list(OUTPUT_DIR.glob("*.json"))) if OUTPUT_DIR.exists() else 0

    filter_sites = sorted({r["site"] for r in rows if r["site"]})
    filter_manufacturers = sorted({r["manufacturer"] for r in rows if r["manufacturer"]})

    return render_template(
        "dashboard.html", rows=rows, pending_count=pending_count, platforms=platforms,
        sites=sites, device_types=device_types,
        filter_sites=filter_sites, filter_manufacturers=filter_manufacturers,
    )


# --------------------------------------------------------------------
# cadastro / edição de device
# --------------------------------------------------------------------

@app.route("/device/new", methods=["GET", "POST"])
@login_required
def device_new():
    nb = get_nb()

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        site_id = request.form.get("site_id")
        role_id = request.form.get("role_id")
        device_type_id = request.form.get("device_type_id")
        if not (name and site_id and role_id and device_type_id):
            flash("Nome, Site, Role e Device Type são obrigatórios.", "error")
            return redirect(url_for("device_new"))
        try:
            device = core.create_device(nb, name, int(site_id), int(role_id), int(device_type_id))
            _apply_discovery_form(nb, device, request.form)
            flash(f"Device '{name}' cadastrado.", "success")
            return redirect(url_for("dashboard"))
        except Exception as exc:
            flash(f"Erro ao cadastrar device: {exc}", "error")
            return redirect(url_for("device_new"))

    sites = list(nb.dcim.sites.all())
    roles = list(nb.dcim.device_roles.all())
    device_types = list(nb.dcim.device_types.all())
    platforms = list(nb.dcim.platforms.all())
    if not (sites and roles and device_types):
        flash(
            "Ainda não existe nenhum Site, Device Role ou Device Type cadastrado "
            "no NetBox. Peça pra alguém técnico criar ao menos um de cada antes "
            "(NetBox > Organização/Dispositivos) -- essa parte é única por "
            "instalação, não precisa repetir por device.",
            "error",
        )
    if not platforms:
        flash(
            "Nenhuma Platform cadastrada no NetBox ainda -- opcional, só "
            "necessário se quiser FORÇAR manualmente como conectar via SSH "
            "num device específico (o normal é resolver sozinho pelo "
            "Fabricante). Crie em NetBox > Devices > Platforms > Add, com o "
            "Slug igual a um device_type válido do Netmiko (ex: 'huawei_vrp', "
            "'cisco_ios', 'juniper_junos').",
            "error",
        )

    return render_template(
        "device_form.html", mode="new", device=None, cf={},
        sites=sites, roles=roles, device_types=device_types, platforms=platforms,
    )


@app.route("/device/<int:device_id>/edit", methods=["GET", "POST"])
@login_required
def device_edit(device_id):
    nb = get_nb()
    device = nb.dcim.devices.get(device_id)
    if not device:
        flash("Device não encontrado.", "error")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        try:
            _apply_discovery_form(nb, device, request.form)
            flash(f"Device '{device.name}' atualizado.", "success")
            return redirect(url_for("dashboard"))
        except Exception as exc:
            flash(f"Erro ao atualizar device: {exc}", "error")
            return redirect(url_for("device_edit", device_id=device_id))

    platforms = list(nb.dcim.platforms.all())
    if not platforms:
        flash(
            "Nenhuma Platform cadastrada no NetBox ainda -- opcional, só "
            "necessário se quiser FORÇAR manualmente como conectar via SSH "
            "num device específico (o normal é resolver sozinho pelo "
            "Fabricante). Crie em NetBox > Devices > Platforms > Add, com o "
            "Slug igual a um device_type válido do Netmiko (ex: 'huawei_vrp', "
            "'cisco_ios', 'juniper_junos').",
            "error",
        )
    cf = device.custom_fields or {}
    return render_template(
        "device_form.html", mode="edit", device=device, cf=cf,
        sites=[], roles=[], device_types=[], platforms=platforms,
    )


@app.route("/device/<int:device_id>/inline-update", methods=["POST"])
@login_required
def device_inline_update(device_id):
    """Endpoint AJAX usado pela edição inline (mesma linha) do dashboard
    -- aplica nome/site/device type/platform/IP/credenciais de
    descoberta sem sair da página."""
    try:
        nb = get_nb()
        device = nb.dcim.devices.get(device_id)
        if not device:
            return {"ok": False, "error": "Device não encontrado."}, 404
        _apply_discovery_form(nb, device, request.form)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}, 400


@app.route("/device/<int:device_id>/row")
@login_required
def device_row(device_id):
    """Devolve o HTML de UMA linha só do dashboard (mesmo template usado
    dentro do loop de dashboard.html) -- chamado pelo JS depois de salvar
    uma edição inline, pra atualizar só aquela linha (checkbox "pronto"/
    Status, e o que mais tiver mudado) sem recarregar a página inteira e
    perder o scroll/estado de edição das outras linhas."""
    nb = get_nb()
    device = nb.dcim.devices.get(device_id)
    if not device:
        return "", 404
    row = _build_row(device)
    return render_template(
        "_device_row.html", r=row,
        sites=_load_sites(nb), device_types=_load_device_types(nb),
        platforms=list(nb.dcim.platforms.all()),
    )


def _apply_discovery_form(nb, device, form):
    """Lê os campos comuns ao formulário de novo/editar device e grava
    Nome, Platform, Primary IP e os custom fields de descoberta.

    Não existe mais escolha manual de "Método" (SSH/SNMP/os dois) -- o
    operador só preenche as credenciais de qualquer protocolo que
    quiser usar (usuário/senha/porta SSH e/ou community SNMP) e o
    método é DERIVADO sozinho a partir do que está preenchido (ver
    has_ssh/has_snmp abaixo). Isso troca o antigo clique-pra-escolher
    (removido do dashboard.html/_device_row.html e do device_form.html)
    por "estilo Zabbix": os campos de credencial estão sempre visíveis,
    e assim que uma credencial fica completa a gente já testa de
    verdade se dá pra conectar (ver test_ssh_connectivity()/
    test_snmp_connectivity() em discovery_core.py) pra colorir o badge
    daquele protocolo verde/vermelho no dashboard."""
    cf_before = dict(device.custom_fields or {})
    old_ip = str(device.primary_ip4).split("/")[0] if device.primary_ip4 else None

    new_name = form.get("name", "").strip()
    if new_name and new_name != device.name:
        device.update({"name": new_name})

    site_id = form.get("site_id")
    if site_id:
        device.update({"site": int(site_id)})

    device_type_id = form.get("device_type_id")
    if device_type_id:
        device.update({"device_type": int(device_type_id)})

    platform_id = form.get("platform_id")
    if platform_id:
        device.update({"platform": int(platform_id)})

    primary_ip = form.get("primary_ip", "").strip()
    ip_changed = False
    if primary_ip:
        ip_changed = primary_ip != old_ip
        core.set_primary_ip(nb, device, primary_ip)

    # discovery_ssh_port é custom field tipo "integer" no NetBox -- manda
    # como string (formulário HTML sempre manda string) que ele recusa com
    # 400 "Value must be an integer". Converte pra int de verdade aqui, e
    # ignora silenciosamente se vier algo não numérico (não trava o salvamento
    # dos outros campos por causa disso).
    raw_ssh_port = (form.get("discovery_ssh_port") or "").strip()
    ssh_port = None
    if raw_ssh_port:
        try:
            ssh_port = int(raw_ssh_port)
        except ValueError:
            ssh_port = None

    has_cred_fields = any(
        k in form for k in
        ("discovery_username", "discovery_password", "discovery_snmp_community", "discovery_ssh_port")
    )
    if has_cred_fields:
        new_username = (form.get("discovery_username") or "").strip() or None
        new_password_raw = form.get("discovery_password") or None  # branco = manter a senha atual
        new_community = (form.get("discovery_snmp_community") or "").strip() or None

        old_username = cf_before.get("discovery_username")
        old_has_password = bool(cf_before.get("discovery_password"))
        old_community = cf_before.get("discovery_snmp_community")
        old_ssh_port = cf_before.get("discovery_ssh_port")

        final_username = new_username if new_username is not None else old_username
        final_has_password = bool(new_password_raw) or old_has_password
        final_community = new_community if new_community is not None else old_community
        final_ssh_port = ssh_port if ssh_port is not None else old_ssh_port

        has_ssh = bool(final_username and final_has_password)
        has_snmp = bool(final_community)
        method = "both" if (has_ssh and has_snmp) else ("ssh" if has_ssh else ("snmp" if has_snmp else None))

        core.set_discovery_fields(
            device, method,
            discovery_username=new_username,
            discovery_password=new_password_raw,
            discovery_snmp_community=new_community,
            discovery_ssh_port=ssh_port,
        )

        # Checagem rápida de conectividade (estilo Zabbix) -- só roda de
        # novo quando os campos DAQUELE protocolo especificamente
        # mudaram (ou nunca foi testado ainda), pra não deixar toda e
        # qualquer edição inline (ex: só renomear o device) mais lenta
        # com um teste de rede desnecessário. Mudar o IP de gerência
        # invalida os dois testes (o alvo mudou), então reroda ambos.
        ssh_fields_changed = (
            (new_username is not None and new_username != old_username)
            or bool(new_password_raw)
            or (ssh_port is not None and ssh_port != old_ssh_port)
            or ip_changed
        )
        snmp_fields_changed = (
            (new_community is not None and new_community != old_community)
            or ip_changed
        )
        never_tested_ssh = not cf_before.get("discovery_ssh_status")
        never_tested_snmp = not cf_before.get("discovery_snmp_status")

        ssh_status = ssh_status_detail = None
        snmp_status = snmp_status_detail = None

        # SSH e SNMP rodam em paralelo (threads) quando os DOIS precisam
        # ser retestados na mesma edição (ex: trocou o IP de gerência) --
        # cada teste é I/O puro (socket/subprocess, libera o GIL), então
        # rodar junto corta o pior caso de "soma dos dois timeouts" (até
        # uns 9s em sequência) pra "o maior dos dois" (uns 6s) -- sentido
        # como demora perceptível a menos numa edição inline, que devia
        # parecer rápida (ver reclamação de "fica parecendo que recarregou
        # a página" -- essa espera parada, sem nenhum aviso, era a causa
        # principal disso).
        def _run_ssh_test():
            nonlocal ssh_status, ssh_status_detail
            final_password_plain = new_password_raw or core.decrypt_secret(cf_before.get("discovery_password"))
            ok, detail = core.test_ssh_connectivity(device, final_username, final_password_plain, final_ssh_port)
            ssh_status, ssh_status_detail = ("ok" if ok else "error"), detail

        def _run_snmp_test():
            nonlocal snmp_status, snmp_status_detail
            ok, detail = core.test_snmp_connectivity(device, final_community)
            snmp_status, snmp_status_detail = ("ok" if ok else "error"), detail

        threads = []
        if has_ssh:
            if ssh_fields_changed or never_tested_ssh:
                threads.append(threading.Thread(target=_run_ssh_test))
        elif cf_before.get("discovery_ssh_status"):
            ssh_status, ssh_status_detail = "", ""

        if has_snmp:
            if snmp_fields_changed or never_tested_snmp:
                threads.append(threading.Thread(target=_run_snmp_test))
        elif cf_before.get("discovery_snmp_status"):
            snmp_status, snmp_status_detail = "", ""

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if ssh_status is not None or snmp_status is not None:
            core.set_connectivity_status(
                device,
                ssh_status=ssh_status, ssh_status_detail=ssh_status_detail,
                snmp_status=snmp_status, snmp_status_detail=snmp_status_detail,
            )


# --------------------------------------------------------------------
# disparar descoberta
# --------------------------------------------------------------------

@app.route("/discover", methods=["POST"])
@login_required
def discover():
    device_ids = request.form.getlist("device_ids")
    if not device_ids:
        flash("Selecione ao menos um device.", "error")
        return redirect(url_for("dashboard"))

    nb = get_nb()
    OUTPUT_DIR.mkdir(exist_ok=True)

    ok, skipped = [], []
    for did in device_ids:
        device = nb.dcim.devices.get(int(did))
        if not device:
            continue
        cf = device.custom_fields or {}
        method = cf.get("discovery_method")
        if not method:
            skipped.append(f"{device.name} (sem método configurado)")
            continue

        result = core.collect_device(device, method, cf, snmp_timeout=SNMP_TIMEOUT, snmp_retries=SNMP_RETRIES)
        result["device_id"] = device.id
        result["device_name"] = device.name
        result["collected_at"] = datetime.now(timezone.utc).isoformat()

        if result.get("errors") and not result.get("interfaces") and not result.get("sys_name"):
            skipped.append(f"{device.name} ({'; '.join(result['errors'])})")
            continue

        out_path = OUTPUT_DIR / f"{device.name}.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        ok.append(device.name)

    if ok:
        flash(f"Descoberta concluída em {len(ok)} device(s): {', '.join(ok)}. Revise antes de aprovar.", "success")
    if skipped:
        flash(f"Pulados: {', '.join(skipped)}", "error")

    return redirect(url_for("review"))


# --------------------------------------------------------------------
# revisão / aprovação
# --------------------------------------------------------------------

@app.route("/review")
@login_required
def review():
    # Usado pra buscar a descrição que já está salva de verdade no NetBox
    # por interface -- best-effort: se o NetBox estiver fora do ar nesse
    # momento, a revisão continua funcionando só com o que veio da
    # descoberta (ver try/except abaixo).
    try:
        nb = get_nb()
    except Exception:
        nb = None

    items = []
    if OUTPUT_DIR.exists():
        for f in sorted(OUTPUT_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            interfaces = data.get("interfaces", [])
            all_names = [i.get("name") for i in interfaces if i.get("name")]

            # Interfaces já existentes no NetBox pra esse device -- uma
            # chamada só (não uma por interface), pra não gerar N+1 em
            # devices com dezenas/centenas de interfaces. O IP já
            # atribuído no NetBox não é mais buscado aqui: a ferramenta
            # existe pra ALIMENTAR o NetBox, então o que importa na
            # revisão é o IP que a descoberta encontrou na porta (coluna
            # "IP (descoberto)"), não o que já está lá.
            existing_by_norm = {}
            device_id = data.get("device_id")
            if nb is not None and device_id:
                try:
                    for ei in nb.dcim.interfaces.filter(device_id=device_id):
                        existing_by_norm[core.normalize_ifname(ei.name)] = ei
                except Exception:
                    existing_by_norm = {}

            for iface in interfaces:
                # Chute de tipo/flag de VLAN calculados aqui (não gravados
                # no JSON em disco) -- assim uma descoberta que já estava
                # pendente antes dessa mudança também ganha a sugestão sem
                # precisar rodar de novo.
                iface["guessed_type"] = core.guess_interface_type(iface.get("name"))
                iface["is_vlan"] = core.is_vlan_ifname(iface.get("name"))
                # Prioridade do palpite de interface pai: 1) confirmado de
                # verdade lendo o device (SSH extra no MikroTik, ver
                # discovery_core.collect_mikrotik_vlan_parents) -- 2) chute
                # por nome (sub-interface dotted-notation tipo Gi0/1.100,
                # ver guess_parent_name) -- 3) nenhum, o operador escolhe.
                iface["guessed_parent"] = (
                    iface.get("vlan_parent_from_device")
                    or (core.guess_parent_name(iface.get("name"), all_names) if iface["is_vlan"] else None)
                )

                # LAG detectada de verdade lendo o device (Eth-Trunk no
                # Huawei via _huawei_collect_eth_trunk, "Parent-LAG" no
                # Datacom via _collect_datacom -- ambos gravam
                # iface["lag_parent"] em discovery_core.py) tem
                # prioridade; se essa rodada de descoberta não trouxe
                # essa informação (ex: comando falhou nesse device dessa
                # vez), cai pro que já estiver salvo no NetBox, pra não
                # perder um vínculo já confirmado antes. Campo livre (não
                # dropdown) porque a LAG pode não existir ainda como
                # Interface no NetBox -- apply_device_result cria se
                # precisar (ver terceira passada).
                existing_if = existing_by_norm.get(core.normalize_ifname(iface.get("name")))
                existing_lag = getattr(existing_if, "lag", None) if existing_if else None
                iface["existing_lag"] = existing_lag.name if existing_lag else ""
                iface["guessed_lag"] = iface.get("lag_parent") or iface["existing_lag"]

                # Descrição REAL já salva no NetBox pra essa interface
                # (quando ela já existe) -- a descrição que vem da própria
                # descoberta (SNMP ifDescr, por exemplo) costuma ser só o
                # nome cru da porta, não o comentário que o operador já
                # colocou no NetBox, então priorizamos o que já está salvo
                # no campo de edição (o operador ainda pode trocar).
                # (existing_if já resolvido acima, junto com existing_lag)
                iface["existing_description"] = existing_if.description if existing_if else ""

            # Agrupa por tipo (físicas por velocidade crescente, depois
            # LAG/bridge/virtual) e ordena por nome dentro do mesmo tipo --
            # antes disso a ordem era a que veio da coleta (walk SNMP ou
            # lista do TextFSM), que não segue tipo nem nome.
            interfaces.sort(key=core.interface_sort_key)

            n_up = sum(1 for i in interfaces if i.get("oper_status") == "up")
            items.append({
                "filename": f.name, "data": data, "n_interfaces": len(interfaces), "n_up": n_up,
                "all_names": all_names,
            })
    return render_template("review.html", items=items, type_choices=core.INTERFACE_TYPE_CHOICES)


def _safe_output_path(filename):
    path = (OUTPUT_DIR / filename).resolve()
    if OUTPUT_DIR.resolve() not in path.parents:
        return None
    return path


def _is_ajax():
    """A tela de Revisão manda essa header via fetch() (ver review.html)
    pra processar Aprovar/Descartar sem recarregar a página inteira --
    assim a confirmação de um device não some quando o operador revisa
    o próximo (cada recarregamento de página derruba o flash message
    anterior). Requests normais de formulário (JS desabilitado, ou
    qualquer outro chamador) continuam com o fluxo flash+redirect de
    sempre."""
    return request.headers.get("X-Requested-With") == "fetch"


def _device_success_message(device_name, changes):
    """Monta a mensagem de sucesso com o nome do device em destaque
    (classe .text-accent, cor teal) -- usada tanto no flash tradicional
    quanto na resposta JSON pro fetch() da tela de Revisão, pra manter
    a mesma cara nos dois casos."""
    msg = Markup(f'<span class="text-accent">{escape(device_name)}</span> enviado com sucesso ao NetBox')
    if changes:
        msg += Markup(f" ({escape(', '.join(changes))})")
    return msg


@app.route("/review/apply/<path:filename>", methods=["POST"])
@login_required
def review_apply(filename):
    ajax = _is_ajax()

    path = _safe_output_path(filename)
    if not path or not path.exists():
        msg = "Arquivo de descoberta não encontrado (talvez já tenha sido processado)."
        if ajax:
            return {"ok": False, "message": msg}, 404
        flash(msg, "error")
        return redirect(url_for("review"))

    data = json.loads(path.read_text(encoding="utf-8"))
    nb = get_nb()
    device = nb.dcim.devices.get(data.get("device_id")) if data.get("device_id") else None
    if not device:
        msg = f"Device {data.get('device_name', '?')} não existe mais no NetBox."
        if ajax:
            return {"ok": False, "message": msg}, 404
        flash(msg, "error")
        return redirect(url_for("review"))

    interface_filter = {}
    idx = 0
    while f"iface_name_{idx}" in request.form:
        name = request.form[f"iface_name_{idx}"]
        include = request.form.get(f"include_{idx}") == "on"
        desc = request.form.get(f"desc_{idx}", "")
        iface_type = request.form.get(f"type_{idx}", "").strip()
        parent_name = request.form.get(f"parent_{idx}", "").strip()
        lag_name = request.form.get(f"lag_{idx}", "").strip()
        interface_filter[name] = {
            "include": include,
            "description": desc,
            "type": iface_type or None,
            "parent_name": parent_name or None,
            "lag_name": lag_name or None,
        }
        idx += 1

    # A gravação no NetBox pode falhar por motivos fora do nosso
    # controle (ex: API rejeitando um campo específico com 400 -- já
    # aconteceu com serial e com type/parent de VLAN) -- sem esse
    # try/except, uma exceção aqui virava um 500 genérico do Flask (a
    # "Internal Server Error" sem detalhe nenhum pro operador, só
    # visível no log do container). Agora a mensagem de erro real
    # aparece na tela.
    try:
        changes = core.apply_device_result(nb, device, data, interface_filter=interface_filter)
    except Exception as exc:
        msg = f"Erro ao gravar {device.name} no NetBox: {exc}"
        if ajax:
            return {"ok": False, "message": msg}, 500
        flash(msg, "error")
        return redirect(url_for("review"))

    APPLIED_DIR.mkdir(exist_ok=True)
    path.rename(APPLIED_DIR / path.name)

    # Mensagem inequívoca de sucesso primeiro (o operador quer confirmar
    # rápido que foi gravado), com o detalhamento técnico entre
    # parênteses só como complemento, e o nome do device em destaque
    # (ver _device_success_message).
    msg = _device_success_message(device.name, changes)

    if ajax:
        return {"ok": True, "message": str(msg)}

    flash(msg, "success")
    return redirect(url_for("review"))


@app.route("/review/discard/<path:filename>", methods=["POST"])
@login_required
def review_discard(filename):
    ajax = _is_ajax()

    path = _safe_output_path(filename)
    if path and path.exists():
        DISCARDED_DIR.mkdir(exist_ok=True)
        path.rename(DISCARDED_DIR / path.name)
        msg = f"Descoberta de '{path.stem}' descartada (nada foi gravado no NetBox)."
        if ajax:
            return {"ok": True, "message": msg}
        flash(msg, "success")
        return redirect(url_for("review"))

    if ajax:
        return {"ok": False, "message": "Arquivo de descoberta não encontrado (talvez já tenha sido processado)."}, 404
    return redirect(url_for("review"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5050")), debug=False)
