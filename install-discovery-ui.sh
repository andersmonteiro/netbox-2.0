#!/usr/bin/env bash
# ==========================================================================
# install-discovery-ui.sh
#
# Instalação STANDALONE da interface web de descoberta -- só o container
# discovery-ui, sem subir NetBox (pra clientes que já têm um NetBox
# rodando, deste template ou não). Pensado pra rodar direto no cliente:
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
# Opcionais:
#   DISCOVERY_UI_USER      -> default: admin
#   DISCOVERY_UI_PASSWORD  -> default: gera senha aleatória
#   TEMPLATE_REPO_URL      -> default: https://github.com/andersmonteiro/netbox-2.0.git
#   INSTALL_DIR            -> default: /opt/netbox-2.0
# ==========================================================================
set -euo pipefail

TEMPLATE_REPO_URL="${TEMPLATE_REPO_URL:-https://github.com/andersmonteiro/netbox-2.0.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/netbox-2.0}"

log()  { echo -e "\n==> $1"; }
warn() { echo -e "\n!!  $1" >&2; }

if [ -z "${NETBOX_URL:-}" ] || [ -z "${NETBOX_TOKEN:-}" ]; then
    echo "!!  NETBOX_URL e NETBOX_TOKEN são obrigatórios pra essa instalação" >&2
    echo "    (ela não sobe um NetBox novo -- só aponta pro que já existe)." >&2
    echo "" >&2
    echo "    export NETBOX_URL='http://IP_DO_NETBOX:8000'" >&2
    echo "    export NETBOX_TOKEN='token-de-api-com-permissao-de-escrita'" >&2
    echo "    curl -fsSL https://raw.githubusercontent.com/andersmonteiro/netbox-2.0/main/install-discovery-ui.sh | bash" >&2
    exit 1
fi

# --------------------------------------------------------------------
# sudo helper
# --------------------------------------------------------------------
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        echo "Rode como root, ou instale o pacote sudo antes de continuar." >&2
        exit 1
    fi
fi
CURRENT_USER="${SUDO_USER:-$USER}"

# --------------------------------------------------------------------
# 1. Docker Engine + Compose plugin
# --------------------------------------------------------------------
log "1/4 Verificando Docker..."
if ! command -v docker >/dev/null 2>&1; then
    echo "    Docker não encontrado, instalando via script oficial (get.docker.com)..."
    curl -fsSL https://get.docker.com | $SUDO sh
else
    echo "    Docker já instalado ($(docker --version))."
fi
$SUDO systemctl enable --now docker 2>/dev/null || warn "Não foi possível habilitar o serviço docker via systemctl (verifique manualmente)."
if ! docker compose version >/dev/null 2>&1; then
    warn "'docker compose' (plugin) não respondeu. Se a instalação via get.docker.com falhou, instale o pacote docker-compose-plugin manualmente."
fi
if ! id -nG "$CURRENT_USER" 2>/dev/null | grep -qw docker; then
    $SUDO usermod -aG docker "$CURRENT_USER" || true
fi

# --------------------------------------------------------------------
# 2. Garantir que temos o template localmente (só precisamos de
# automation-scripts/, discovery-ui/ e docker-compose.discovery-ui.yml,
# mas é mais simples clonar o repo inteiro, igual ao bootstrap.sh)
# --------------------------------------------------------------------
log "2/4 Localizando/obtendo o template..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd || pwd)"
if [ -f "$SCRIPT_DIR/docker-compose.discovery-ui.yml" ]; then
    echo "    Já estamos dentro do template ($SCRIPT_DIR), usando esta pasta."
    REPO_DIR="$SCRIPT_DIR"
else
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "    $INSTALL_DIR já existe, sincronizando com o repositório remoto..."
        git -C "$INSTALL_DIR" fetch --quiet origin
        DEFAULT_BRANCH="$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
        git -C "$INSTALL_DIR" reset --hard "origin/${DEFAULT_BRANCH}"
    else
        echo "    Clonando $TEMPLATE_REPO_URL para $INSTALL_DIR"
        $SUDO mkdir -p "$(dirname "$INSTALL_DIR")"
        $SUDO git clone "$TEMPLATE_REPO_URL" "$INSTALL_DIR"
        $SUDO chown -R "$CURRENT_USER":"$CURRENT_USER" "$INSTALL_DIR"
    fi
    REPO_DIR="$INSTALL_DIR"
fi
cd "$REPO_DIR"

# --------------------------------------------------------------------
# 3. .env.discovery-ui -- NETBOX_URL/NETBOX_TOKEN vêm de fora (client já
# existente); login/senha/chave da tela são gerados automaticamente.
# --------------------------------------------------------------------
log "3/4 Preparando credenciais..."
ENV_FILE="$REPO_DIR/.env.discovery-ui"
if [ ! -f "$ENV_FILE" ]; then
    cp "$REPO_DIR/.env.discovery-ui.example" "$ENV_FILE"
fi

sed -i "s|^NETBOX_URL=.*|NETBOX_URL=${NETBOX_URL}|" "$ENV_FILE"
sed -i "s|^NETBOX_TOKEN=.*|NETBOX_TOKEN=${NETBOX_TOKEN}|" "$ENV_FILE"

if [ -n "${DISCOVERY_UI_USER:-}" ]; then
    sed -i "s|^DISCOVERY_UI_USER=.*|DISCOVERY_UI_USER=${DISCOVERY_UI_USER}|" "$ENV_FILE"
fi

if grep -q "^DISCOVERY_UI_PASSWORD=troque-esta-senha$" "$ENV_FILE" 2>/dev/null; then
    if [ -n "${DISCOVERY_UI_PASSWORD:-}" ]; then
        FINAL_PASSWORD="$DISCOVERY_UI_PASSWORD"
        echo "    Usando DISCOVERY_UI_PASSWORD fornecido via variável de ambiente."
    else
        FINAL_PASSWORD="$(openssl rand -base64 18 | tr -d '=+/')"
    fi
    sed -i "s|^DISCOVERY_UI_PASSWORD=.*|DISCOVERY_UI_PASSWORD=${FINAL_PASSWORD}|" "$ENV_FILE"
fi
if grep -q "^DISCOVERY_UI_SECRET_KEY=troque-esta-chave-aleatoria$" "$ENV_FILE" 2>/dev/null; then
    FINAL_SECRET_KEY="$(openssl rand -hex 24)"
    sed -i "s|^DISCOVERY_UI_SECRET_KEY=.*|DISCOVERY_UI_SECRET_KEY=${FINAL_SECRET_KEY}|" "$ENV_FILE"
fi

# --------------------------------------------------------------------
# 4. Build + up (só o container discovery-ui)
# --------------------------------------------------------------------
log "4/4 Build + subida do container..."
docker compose -f docker-compose.discovery-ui.yml --env-file "$ENV_FILE" --progress=tty up -d --build < /dev/null

SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

==========================================================================
Instalação concluída!

Descoberta de rede (interface web, apontando pro NetBox informado):
NetBox:        ${NETBOX_URL}
discovery-ui:  http://${SERVER_IP:-SEU_SERVIDOR}:5050
Usuário:       $(grep '^DISCOVERY_UI_USER=' "$ENV_FILE" | cut -d= -f2)
Senha:         $(grep '^DISCOVERY_UI_PASSWORD=' "$ENV_FILE" | cut -d= -f2)

*** Anote a senha acima agora — ela não aparece de novo. ***

Antes de usar, confirme que os Custom Fields de descoberta já existem
nesse NetBox (rode uma vez, se ainda não rodou):
  cd $REPO_DIR/automation-scripts
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  NETBOX_URL=${NETBOX_URL} NETBOX_TOKEN=${NETBOX_TOKEN} python create_discovery_fields.py
==========================================================================
EOF
