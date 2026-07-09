#!/usr/bin/env python3
"""
encrypt_existing_secrets.py
============================
Migração única: cifra o custom field discovery_password de todo device
que ainda estiver com ele em TEXTO PURO (salvo antes da introdução de
encrypt_secret()/decrypt_secret() em discovery_core.py).

Sem rodar isso, uma senha só é cifrada quando o operador salva ela de
novo pela tela (dashboard ou /device/<id>/edit) -- qualquer senha já
cadastrada antes continua em texto puro no NetBox indefinidamente. Este
script varre TODOS os devices de uma vez e resolve isso sem precisar
reabrir/resalvar cada um manualmente.

Idempotente: um valor que já está cifrado (prefixo "enc:v1:", ver
discovery_core._SECRET_PREFIX) é ignorado -- pode rodar mais de uma vez
sem risco de cifrar em cima de cifrado.

Uso:
    python encrypt_existing_secrets.py            # varre e aplica
    python encrypt_existing_secrets.py --dry-run  # só mostra quem seria alterado

Precisa das mesmas variáveis de ambiente do resto do projeto:
NETBOX_URL, NETBOX_TOKEN, e a MESMA chave usada pela interface web pra
cifrar (FLASK_SECRET_KEY ou DISCOVERY_UI_SECRET_KEY -- ver .env) --
senão o valor gravado por este script não seria decifrável pela app
depois (chaves diferentes = senha "perdida" na prática, precisaria ser
recadastrada). Rode isso no mesmo ambiente/.env que o container
netbox-oracle usa (ex: dentro do container, ou no host lendo o mesmo
.env).
"""

import argparse

from dotenv import load_dotenv

import discovery_core as core

load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        description="Cifra discovery_password de devices que ainda estão com a senha em texto puro no NetBox."
    )
    parser.add_argument("--dry-run", action="store_true", help="Só lista quem seria alterado, não grava nada")
    args = parser.parse_args()

    if core.Fernet is None:
        print(
            "[erro] Pacote 'cryptography' não está instalado nesse ambiente -- "
            "instale com 'pip install cryptography' (ou rode dentro do container "
            "netbox-oracle, que já tem) antes de continuar."
        )
        return

    if core._fernet() is None:
        print(
            "[erro] Não encontrei FLASK_SECRET_KEY nem DISCOVERY_UI_SECRET_KEY no "
            "ambiente/.env -- sem essa chave não dá pra cifrar de forma que a "
            "interface web consiga decifrar depois. Rode isso no mesmo ambiente "
            "(.env) do container netbox-oracle."
        )
        return

    nb = core.get_client()
    devices = list(nb.dcim.devices.all())

    already_encrypted, updated, skipped_empty = 0, 0, 0
    for device in devices:
        cf = device.custom_fields or {}
        raw = cf.get("discovery_password")
        if not raw:
            skipped_empty += 1
            continue
        if isinstance(raw, str) and raw.startswith(core._SECRET_PREFIX):
            already_encrypted += 1
            continue

        new_value = core.encrypt_secret(raw)
        print(f"[{'seria cifrado' if args.dry_run else 'cifrado'}] {device.name}")
        if not args.dry_run:
            new_cf = dict(cf)
            new_cf["discovery_password"] = new_value
            device.update({"custom_fields": new_cf})
        updated += 1

    print()
    print(f"Total de devices: {len(devices)}")
    print(f"  já cifrados antes: {already_encrypted}")
    print(f"  sem senha configurada: {skipped_empty}")
    print(f"  {'seriam cifrados agora' if args.dry_run else 'cifrados agora'}: {updated}")
    if args.dry_run and updated:
        print("\nRode sem --dry-run pra aplicar de verdade.")


if __name__ == "__main__":
    main()
