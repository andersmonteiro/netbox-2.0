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


def get_nb():
    return core.get_client(NETBOX_URL, NETBOX_TOKEN)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


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

    rows = []
    for d in devices:
        cf = d.custom_fields or {}
        method = cf.get("discovery_method")
        has_cred = bool(cf.get("discovery_username") and cf.get("discovery_password")) if method == "ssh" else bool(cf.get("discovery_snmp_community")) if method == "snmp" else False
        rows.append({
            "id": d.id,
            "name": d.name,
            "site": str(d.site) if d.site else "",
            "primary_ip": str(d.primary_ip4).split("/")[0] if d.primary_ip4 else None,
            "platform": str(d.platform) if d.platform else None,
            "method": method,
            "has_cred": has_cred,
            "ready": bool(method and has_cred and d.primary_ip4 and (method != "ssh" or d.platform)),
        })

    pending_count = len(list(OUTPUT_DIR.glob("*.json"))) if OUTPUT_DIR.exists() else 0

    return render_template("dashboard.html", rows=rows, pending_count=pending_count)


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
    cf = device.custom_fields or {}
    return render_template(
        "device_form.html", mode="edit", device=device, cf=cf,
        sites=[], roles=[], device_types=[], platforms=platforms,
    )


def _apply_discovery_form(nb, device, form):
    """Lê os campos comuns ao formulário de novo/editar device e grava
    Platform, Primary IP e os custom fields de descoberta."""
    platform_id = form.get("platform_id")
    if platform_id:
        device.update({"platform": int(platform_id)})

    primary_ip = form.get("primary_ip", "").strip()
    if primary_ip:
        core.set_primary_ip(nb, device, primary_ip)

    method = form.get("method")
    if method:
        core.set_discovery_fields(
            device, method,
            discovery_username=form.get("discovery_username") or None,
            discovery_password=form.get("discovery_password") or None,
            discovery_snmp_community=form.get("discovery_snmp_community") or None,
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
    items = []
    if OUTPUT_DIR.exists():
        for f in sorted(OUTPUT_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            n_up = sum(1 for i in data.get("interfaces", []) if i.get("oper_status") == "up")
            items.append({"filename": f.name, "data": data, "n_interfaces": len(data.get("interfaces", [])), "n_up": n_up})
    return render_template("review.html", items=items)


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
        interface_filter[name] = {"include": include, "description": desc}
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
