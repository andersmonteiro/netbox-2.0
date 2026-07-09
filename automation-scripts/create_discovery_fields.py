#!/usr/bin/env python3
"""
create_discovery_fields.py
===========================
Cria (uma vez, por instalação) os Custom Fields no NetBox usados pelo
`discovery_netbox.py` para saber COMO e COM QUAL CREDENCIAL descobrir
cada Device: método (SSH ou SNMP), usuário/senha (SSH) e community
(SNMP). Depois de rodado, esses campos aparecem normalmente no
formulário de "Adicionar/Editar Device" do próprio NetBox — não precisa
editar YAML nem tocar em outro lugar pra cadastrar um device novo.

Idempotente: pode rodar de novo sem duplicar (verifica se cada campo já
existe antes de criar).

AVISO DE SEGURANÇA: estes campos NÃO são criptografados (custom field
comum do NetBox community, sem plugin de secrets). Qualquer usuário com
permissão de ver o Device vê a senha/community em texto. Se isso for um
problema pro seu ambiente, avalie o plugin `netbox-secrets` em vez
deste script.

Uso:
    python create_discovery_fields.py

Variáveis de ambiente: NETBOX_URL, NETBOX_TOKEN (ou .env)
"""

import os
import sys

import pynetbox
from dotenv import load_dotenv

load_dotenv()

NETBOX_URL = os.environ.get("NETBOX_URL")
NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")

# Nome interno (usado na API/objects) -> definição do campo.
FIELDS = {
    "discovery_method": {
        "type": "select",
        "label": "Descoberta: método",
        "description": "Como o discovery_netbox.py deve descobrir este device (deixe em branco para não incluir na descoberta).",
        "choice_set": {
            "extra_choices": [["ssh", "SSH"], ["snmp", "SNMP"], ["both", "SSH + SNMP"]],
        },
        "weight": 100,
    },
    "discovery_username": {
        "type": "text",
        "label": "Descoberta: usuário (SSH)",
        "description": "Usuário de SSH, usado quando 'Descoberta: método' = SSH.",
        "weight": 110,
    },
    "discovery_password": {
        "type": "text",
        "label": "Descoberta: senha (SSH)",
        "description": "Senha de SSH, usado quando 'Descoberta: método' = SSH. Texto plano -- ver aviso de segurança no script create_discovery_fields.py.",
        "weight": 120,
    },
    "discovery_ssh_port": {
        "type": "integer",
        "label": "Descoberta: porta SSH",
        "description": "Porta SSH, usado quando 'Descoberta: método' = SSH. Deixe em branco pra usar a porta padrão (22).",
        "weight": 125,
    },
    "discovery_snmp_community": {
        "type": "text",
        "label": "Descoberta: community (SNMP)",
        "description": "Community SNMPv2c (ex: 'public'), usado quando 'Descoberta: método' = SNMP. Texto plano -- ver aviso de segurança no script create_discovery_fields.py.",
        "weight": 130,
    },
}


def get_client() -> pynetbox.api:
    if not NETBOX_URL or not NETBOX_TOKEN:
        sys.exit("Defina NETBOX_URL e NETBOX_TOKEN (variáveis de ambiente ou .env).")
    return pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)


def main():
    nb = get_client()

    for name, spec in FIELDS.items():
        existing = nb.extras.custom_fields.get(name=name)

        choice_set = None
        if "choice_set" in spec:
            # NetBox exige um Custom Field Choice Set separado para
            # campos tipo "select" -- cria (ou reaproveita) um antes.
            choice_set_name = f"{name}_choices"
            choice_set = nb.extras.custom_field_choice_sets.get(name=choice_set_name)
            desired_choices = spec["choice_set"]["extra_choices"]
            if not choice_set:
                choice_set = nb.extras.custom_field_choice_sets.create(
                    {"name": choice_set_name, "extra_choices": desired_choices}
                )
                print(f"[criado] choice set {choice_set_name}")
            else:
                # Instalação já existente, rodando o script de novo depois
                # de uma opção nova ter sido adicionada aqui (ex: "both"
                # pra discovery_method) -- adiciona só o que falta, sem
                # remover nada que já esteja lá (evita quebrar devices já
                # configurados com uma opção antiga).
                current_values = {c[0] for c in (choice_set.extra_choices or [])}
                missing = [c for c in desired_choices if c[0] not in current_values]
                if missing:
                    updated = list(choice_set.extra_choices or []) + missing
                    choice_set.update({"extra_choices": updated})
                    print(f"[atualizado] choice set {choice_set_name}: adicionado {[m[0] for m in missing]}")

        if existing:
            print(f"[já existe] {name}")
            continue

        payload = {
            "object_types": ["dcim.device"],
            "type": spec["type"],
            "name": name,
            "label": spec["label"],
            "description": spec["description"],
            "weight": spec["weight"],
            "required": False,
        }
        if choice_set is not None:
            payload["choice_set"] = choice_set.id

        nb.extras.custom_fields.create(payload)
        print(f"[criado] {name}")

    print(
        "\nPronto. Os campos aparecem em Devices > (editar um device) > "
        "Custom Fields. Preencha 'discovery_method' + a credencial "
        "correspondente nos devices que quiser descobrir, depois rode:\n"
        "  python discovery_netbox.py collect\n"
    )


if __name__ == "__main__":
    main()
