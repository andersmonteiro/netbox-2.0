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
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for

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
            }
        except Exception:
            pass
    return {"netbox_url": NETBOX_URL or "", "netbox_token": NETBOX_TOKEN or ""}


def save_settings(url, token):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps({"netbox_url": url, "netbox_token": token}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


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
_NETBOX_INDEPENDENT_ENDPOINTS = {"login", "logout", "settings_view", "api_status", "static"}


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
        if username == UI_USER and password and password == UI_PASSWORD:
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
        save_settings(url, token)
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
# dashboard
# --------------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    try:
        nb = get_nb()
        devices = sorted(nb.dcim.devices.all(), key=lambda d: d.name or "")
    except Exception as exc:
        flash(f"Erro ao consultar o NetBox: {exc}", "error")
        devices = []

    try:
        platforms = list(nb.dcim.platforms.all())
    except Exception:
        platforms = []

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

    rows = []
    for d in devices:
        cf = d.custom_fields or {}
        method = cf.get("discovery_method")
        has_ssh_cred_pair = bool(cf.get("discovery_username") and cf.get("discovery_password"))
        has_community = bool(cf.get("discovery_snmp_community"))
        if method == "ssh":
            has_cred = has_ssh_cred_pair
        elif method == "snmp":
            has_cred = has_community
        elif method == "both":
            # "both" precisa das DUAS credenciais -- SSH (NAPALM) e SNMP
            # rodam juntos e os resultados são cruzados por interface (ver
            # discovery_core.collect_both()).
            has_cred = has_ssh_cred_pair and has_community
        else:
            has_cred = False
        # Independente do método -- usado só pra mostrar "definida"/"—" na
        # célula de Senha (SSH), que agora também serve pro extra opcional
        # de MikroTik em devices configurados como SNMP (ver allow_ssh_cred
        # no dashboard.html). Não entra no cálculo de "ready" isolado: a
        # checagem extra do MikroTik é opcional, não bloqueia a descoberta
        # principal -- mas pra method="both" ela É obrigatória (embutida
        # no has_cred acima).
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
        rows.append({
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
            # Lista pra montar os "chips" (SSH / SNMP) no dashboard --
            # method="both" vira os dois chips, "ssh"/"snmp" vira um só.
            "method_list": (["ssh", "snmp"] if method == "both" else [method] if method else []),
            "has_cred": has_cred,
            "has_ssh_cred": has_ssh_cred,
            # Platform (driver NAPALM) só é obrigatório quando o método
            # envolve SSH ("ssh" ou "both") -- SNMP puro não precisa.
            "ready": bool(method and has_cred and d.primary_ip4 and (method not in ("ssh", "both") or d.platform)),
            "cf_username": cf.get("discovery_username") or "",
            "cf_ssh_port": cf.get("discovery_ssh_port") or "",
            "cf_snmp_community": cf.get("discovery_snmp_community") or "",
        })

    pending_count = len(list(OUTPUT_DIR.glob("*.json"))) if OUTPUT_DIR.exists() else 0

    filter_sites = sorted({r["site"] for r in rows if r["site"]})
    filter_regions = sorted({r["region"] for r in rows if r["region"]})
    filter_manufacturers = sorted({r["manufacturer"] for r in rows if r["manufacturer"]})

    return render_template(
        "dashboard.html", rows=rows, pending_count=pending_count, platforms=platforms,
        sites=sites, device_types=device_types,
        filter_sites=filter_sites, filter_regions=filter_regions, filter_manufacturers=filter_manufacturers,
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
            "Nenhuma Platform cadastrada no NetBox ainda -- necessário só pro "
            "método SSH (NAPALM). Crie em NetBox > Devices > Platforms > Add, "
            "com o Slug igual ao nome do driver NAPALM do fabricante (ex: "
            "'huawei_vrp', 'cisco_ios', 'juniper_junos').",
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
            "Nenhuma Platform cadastrada no NetBox ainda -- necessário só pro "
            "método SSH (NAPALM). Crie em NetBox > Devices > Platforms > Add, "
            "com o Slug igual ao nome do driver NAPALM do fabricante (ex: "
            "'huawei_vrp', 'cisco_ios', 'juniper_junos').",
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


def _apply_discovery_form(nb, device, form):
    """Lê os campos comuns ao formulário de novo/editar device e grava
    Nome, Platform, Primary IP e os custom fields de descoberta."""
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
    if primary_ip:
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

    # "method" in form (não só truthy) porque o widget de chips do
    # dashboard manda method="" de propósito quando o operador remove
    # todos os protocolos (os dois "x") -- precisa chegar até aqui pra
    # limpar discovery_method de verdade no NetBox, não só ignorar. Os
    # outros formulários (cadastro/edição de device) simplesmente não
    # mandam esse campo quando não se aplica, então o "in" não muda nada
    # pra eles.
    if "method" in form:
        method = form.get("method") or None
        core.set_discovery_fields(
            device, method,
            discovery_username=form.get("discovery_username") or None,
            discovery_password=form.get("discovery_password") or None,
            discovery_snmp_community=form.get("discovery_snmp_community") or None,
            discovery_ssh_port=ssh_port,
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
    # Usado pra buscar o que já está salvo de verdade no NetBox (descrição
    # e IP) por interface -- best-effort: se o NetBox estiver fora do ar
    # nesse momento, a revisão continua funcionando só com o que veio da
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

            # Interfaces e IPs já existentes no NetBox pra esse device --
            # uma chamada pra cada (não uma por interface), pra não gerar
            # N+1 em devices com dezenas/centenas de interfaces.
            existing_by_norm = {}
            ips_by_iface_id = {}
            device_id = data.get("device_id")
            if nb is not None and device_id:
                try:
                    for ei in nb.dcim.interfaces.filter(device_id=device_id):
                        existing_by_norm[core.normalize_ifname(ei.name)] = ei
                    for ip in nb.ipam.ip_addresses.filter(device_id=device_id):
                        aoid = getattr(ip, "assigned_object_id", None)
                        if aoid:
                            ips_by_iface_id.setdefault(aoid, []).append(str(ip.address))
                except Exception:
                    existing_by_norm = {}
                    ips_by_iface_id = {}

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

                # Descrição/IP REAIS já salvos no NetBox pra essa interface
                # (quando ela já existe) -- a descrição que vem da própria
                # descoberta (SNMP ifDescr, por exemplo) costuma ser só o
                # nome cru da porta, não o comentário que o operador já
                # colocou no NetBox, então priorizamos o que já está salvo
                # no campo de edição (o operador ainda pode trocar). O IP
                # é só informativo (não editável aqui -- atribuir IP é
                # outro fluxo).
                existing_if = existing_by_norm.get(core.normalize_ifname(iface.get("name")))
                iface["existing_description"] = existing_if.description if existing_if else ""
                iface["existing_ips"] = ips_by_iface_id.get(existing_if.id, []) if existing_if else []

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


@app.route("/review/apply/<path:filename>", methods=["POST"])
@login_required
def review_apply(filename):
    path = _safe_output_path(filename)
    if not path or not path.exists():
        flash("Arquivo de descoberta não encontrado (talvez já tenha sido processado).", "error")
        return redirect(url_for("review"))

    data = json.loads(path.read_text(encoding="utf-8"))
    nb = get_nb()
    device = nb.dcim.devices.get(data.get("device_id")) if data.get("device_id") else None
    if not device:
        flash(f"Device {data.get('device_name', '?')} não existe mais no NetBox.", "error")
        return redirect(url_for("review"))

    interface_filter = {}
    idx = 0
    while f"iface_name_{idx}" in request.form:
        name = request.form[f"iface_name_{idx}"]
        include = request.form.get(f"include_{idx}") == "on"
        desc = request.form.get(f"desc_{idx}", "")
        iface_type = request.form.get(f"type_{idx}", "").strip()
        parent_name = request.form.get(f"parent_{idx}", "").strip()
        interface_filter[name] = {
            "include": include,
            "description": desc,
            "type": iface_type or None,
            "parent_name": parent_name or None,
        }
        idx += 1

    changes = core.apply_device_result(nb, device, data, interface_filter=interface_filter)

    APPLIED_DIR.mkdir(exist_ok=True)
    path.rename(APPLIED_DIR / path.name)

    msg = f"{device.name}: " + (", ".join(changes) if changes else "sem mudanças")
    flash(msg, "success")
    return redirect(url_for("review"))


@app.route("/review/discard/<path:filename>", methods=["POST"])
@login_required
def review_discard(filename):
    path = _safe_output_path(filename)
    if path and path.exists():
        DISCARDED_DIR.mkdir(exist_ok=True)
        path.rename(DISCARDED_DIR / path.name)
        flash(f"Descoberta de '{path.stem}' descartada (nada foi gravado no NetBox).", "success")
    return redirect(url_for("review"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5050")), debug=False)
