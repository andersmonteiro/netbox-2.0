#!/usr/bin/env python3
"""
discovery_netbox.py
====================
CLI de descoberta: lê Devices do NetBox que tiverem os custom fields de
descoberta preenchidos (ver create_discovery_fields.py), coleta dados
reais via SSH (NAPALM) ou SNMP (SNMPv2c) usando discovery_core.py, grava
o resultado em JSON para revisão humana, e só aplica no NetBox depois de
confirmação explícita.

Existe também uma interface web equivalente (discovery-ui/), pensada pra
quem não usa terminal -- ver seção 2.4 do README. Este CLI continua
funcionando igual, pra automação/cron.

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
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

import discovery_core as core

load_dotenv()

OUTPUT_DIR = Path(__file__).parent / "discovery_output"
APPLIED_DIR = OUTPUT_DIR / "applied"


# --------------------------------------------------------------------
# collect
# --------------------------------------------------------------------

def cmd_collect(args):
    nb = core.get_client()
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

        result = core.collect_device(
            device, method, cf,
            snmp_timeout=args.snmp_timeout, snmp_retries=args.snmp_retries,
        )

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

    nb = core.get_client()
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

        changes = core.apply_device_result(nb, device, data)

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
