#!/usr/bin/env bash
# ==========================================================================
# setup.sh
# Automatiza o passo "clone o netbox-docker oficial + copie os arquivos
# deste template pra dentro dele", pra não precisar repetir manualmente
# em cada cliente. Idempotente: pode rodar de novo pra atualizar.
#
# Uso:
#   ./setup.sh            # clona/atualiza netbox-docker e copia o overlay
#   ./setup.sh --up        # idem, e já sobe a stack (build + up -d)
# ==========================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NETBOX_DOCKER_DIR="$REPO_DIR/netbox-docker"
NETBOX_DOCKER_REPO="https://github.com/netbox-community/netbox-docker.git"

echo "==> Repositório do template: $REPO_DIR"

if [ ! -d "$NETBOX_DOCKER_DIR/.git" ]; then
    echo "==> Clonando netbox-docker (branch release)..."
    git clone -b release "$NETBOX_DOCKER_REPO" "$NETBOX_DOCKER_DIR"
else
    echo "==> netbox-docker já existe, atualizando (git pull)..."
    git -C "$NETBOX_DOCKER_DIR" pull
fi

echo "==> Copiando arquivos de customização para dentro do netbox-docker..."
cp "$REPO_DIR/docker-compose.override.yml" "$NETBOX_DOCKER_DIR/"
cp "$REPO_DIR/Dockerfile-Plugins" "$NETBOX_DOCKER_DIR/"
cp "$REPO_DIR/plugin_requirements.txt" "$NETBOX_DOCKER_DIR/"
mkdir -p "$NETBOX_DOCKER_DIR/configuration"
cp "$REPO_DIR/configuration/plugins.py" "$NETBOX_DOCKER_DIR/configuration/plugins.py"
cp -r "$REPO_DIR/automation-scripts" "$NETBOX_DOCKER_DIR/"
# discovery-ui precisa estar aqui dentro porque o docker compose (rodado
# de dentro de netbox-docker/) usa build.context: "." -- sem isso o
# "COPY discovery-ui/..." do Dockerfile não acha os arquivos.
cp -r "$REPO_DIR/discovery-ui" "$NETBOX_DOCKER_DIR/"

if [ ! -f "$NETBOX_DOCKER_DIR/.env" ]; then
    echo "==> Criando .env a partir de .env.example (edite antes de subir!)"
    cp "$REPO_DIR/.env.example" "$NETBOX_DOCKER_DIR/.env"
    echo "    -> $NETBOX_DOCKER_DIR/.env"
else
    echo "==> .env já existe em netbox-docker/, não sobrescrevi."
fi

echo "==> Pronto. Revise $NETBOX_DOCKER_DIR/.env com os dados reais do cliente."

if [ "${1:-}" == "--up" ]; then
    echo "==> Subindo a stack (build + up -d)..."
    (cd "$NETBOX_DOCKER_DIR" && docker compose build --no-cache && docker compose up -d)
    echo "==> NetBox deve estar disponível em http://localhost:8000 em alguns minutos."
else
    echo "==> Para subir a stack: cd netbox-docker && docker compose build --no-cache && docker compose up -d"
fi
