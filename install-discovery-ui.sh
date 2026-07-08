#!/usr/bin/env bash
# ==========================================================================
# install-discovery-ui.sh
#
# Atalho pra instalação STANDALONE da discovery-ui (só o container de
# descoberta, sem subir NetBox -- pra clientes que já têm um NetBox
# rodando, deste template ou não). Só chama o bootstrap.sh já no modo
# "discovery-only" -- a lógica real mora lá (fonte única, pra não ter
# duas cópias do mesmo fluxo desalinhando com o tempo). Equivalente a
# rodar o bootstrap.sh e escolher a opção 2 quando ele perguntar.
#
#   export NETBOX_URL='http://IP_DO_NETBOX:8000'
#   export NETBOX_TOKEN='token-de-api-com-permissao-de-escrita'
#   curl -fsSL https://raw.githubusercontent.com/andersmonteiro/netbox-2.0/main/install-discovery-ui.sh | bash
#
# Variáveis de ambiente OBRIGATÓRIAS (não tem como o instalador
# adivinhar o NetBox de um cliente que já existe):
#   NETBOX_URL    -> URL do NetBox já existente do cliente
#   NETBOX_TOKEN  -> token de API do NetBox do cliente (permissão de
#                    escrita em dcim/ipam)
#
# Opcionais: DISCOVERY_UI_USER, DISCOVERY_UI_PASSWORD, TEMPLATE_REPO_URL,
# INSTALL_DIR -- ver bootstrap.sh.
# ==========================================================================
set -euo pipefail

export INSTALL_MODE=discovery-only

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd || pwd)"
if [ -f "$SCRIPT_DIR/bootstrap.sh" ]; then
    exec bash "$SCRIPT_DIR/bootstrap.sh"
fi

curl -fsSL https://raw.githubusercontent.com/andersmonteiro/netbox-2.0/main/bootstrap.sh | bash
