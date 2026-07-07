#!/usr/bin/env bash
# ==========================================================================
# bootstrap.sh
#
# Instalação completa em um servidor NOVO (sem Docker, sem nada). Pensado
# para rodar direto no cliente com um único comando:
#
#   curl -fsSL https://raw.githubusercontent.com/andersmonteiro/netbox-2.0/main/bootstrap.sh | bash
#
# O que ele faz:
#   1. Instala dependências de sistema (git, curl, nmap, python3, openssl)
#   2. Instala Docker Engine + Docker Compose plugin (script oficial da
#      Docker), se ainda não estiverem instalados
#   3. Clona este template (se ainda não estiver rodando de dentro dele)
#   4. Roda o setup.sh (clona o netbox-docker oficial + aplica o overlay)
#   5. Gera senha/token do superusuário automaticamente (se ainda forem
#      os valores de exemplo) e sobe a stack com docker compose
#
# Testado em Ubuntu/Debian. Em outras distros, os passos 1-2 podem
# precisar de ajuste manual (o script avisa e continua mesmo assim).
#
# Variáveis de ambiente opcionais:
#   TEMPLATE_REPO_URL  -> default: https://github.com/andersmonteiro/netbox-2.0.git
#   INSTALL_DIR        -> default: /opt/netbox-2.0
# ==========================================================================
set -euo pipefail

TEMPLATE_REPO_URL="${TEMPLATE_REPO_URL:-https://github.com/andersmonteiro/netbox-2.0.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/netbox-2.0}"

log()  { echo -e "\n==> $1"; }
warn() { echo -e "\n!!  $1" >&2; }

# --------------------------------------------------------------------
# sudo helper: usa sudo se não formos root
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
# 1. Dependências de sistema
# --------------------------------------------------------------------
log "1/5 Detectando sistema e instalando dependências básicas..."
if [ -f /etc/os-release ]; then
    . /etc/os-release
    case "${ID:-}" in
        ubuntu|debian) ;;
        *) warn "Testado em Ubuntu/Debian. Sistema detectado: ${PRETTY_NAME:-desconhecido}. Prosseguindo mesmo assim." ;;
    esac
else
    warn "Não foi possível detectar o SO (/etc/os-release ausente). Prosseguindo mesmo assim."
fi

if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -y
    $SUDO apt-get install -y git curl ca-certificates gnupg nmap python3 python3-venv python3-pip openssl
else
    warn "apt-get não encontrado. Instale manualmente: git curl nmap python3 python3-venv python3-pip openssl"
fi

# --------------------------------------------------------------------
# 2. Docker Engine + Compose plugin
# --------------------------------------------------------------------
log "2/5 Verificando Docker..."
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
    echo "    Usuário '$CURRENT_USER' adicionado ao grupo docker."
    echo "    (Precisa de um novo login/'newgrp docker' para valer sem sudo em comandos futuros; este script usa sudo internamente e não depende disso.)"
fi

# --------------------------------------------------------------------
# 3. Garantir que temos o template localmente
# --------------------------------------------------------------------
log "3/5 Localizando/obtendo o template..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/setup.sh" ] && [ -f "$SCRIPT_DIR/docker-compose.override.yml" ]; then
    echo "    Já estamos dentro do template ($SCRIPT_DIR), usando esta pasta."
    REPO_DIR="$SCRIPT_DIR"
else
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "    $INSTALL_DIR já existe, atualizando (git pull)..."
        git -C "$INSTALL_DIR" pull
    else
        echo "    Clonando $TEMPLATE_REPO_URL para $INSTALL_DIR"
        $SUDO mkdir -p "$(dirname "$INSTALL_DIR")"
        $SUDO git clone "$TEMPLATE_REPO_URL" "$INSTALL_DIR"
        $SUDO chown -R "$CURRENT_USER":"$CURRENT_USER" "$INSTALL_DIR"
    fi
    REPO_DIR="$INSTALL_DIR"
fi
cd "$REPO_DIR"
chmod +x setup.sh bootstrap.sh 2>/dev/null || true

# --------------------------------------------------------------------
# 4. setup.sh: clona netbox-docker oficial + aplica overlay + cria .env
# --------------------------------------------------------------------
log "4/5 Preparando a stack (netbox-docker oficial + overlay deste template)..."
./setup.sh

ENV_FILE="$REPO_DIR/netbox-docker/.env"

# Gera automaticamente senha/token se ainda estiverem com o valor de
# exemplo (evita subir em produção com credencial padrão previsível).
if grep -q "troque-esta-senha" "$ENV_FILE" 2>/dev/null; then
    NEW_PASSWORD="$(openssl rand -base64 18 | tr -d '=+/')"
    sed -i "s|SUPERUSER_PASSWORD=troque-esta-senha|SUPERUSER_PASSWORD=${NEW_PASSWORD}|" "$ENV_FILE"
fi
if grep -q "troque-este-token-de-40-caracteres" "$ENV_FILE" 2>/dev/null; then
    NEW_TOKEN="$(openssl rand -hex 20)"
    sed -i "s|SUPERUSER_API_TOKEN=troque-este-token-de-40-caracteres|SUPERUSER_API_TOKEN=${NEW_TOKEN}|" "$ENV_FILE"
    sed -i "s|NETBOX_TOKEN=\${SUPERUSER_API_TOKEN}|NETBOX_TOKEN=${NEW_TOKEN}|" "$ENV_FILE"
    sed -i "s|MCP_NETBOX_TOKEN=\${SUPERUSER_API_TOKEN}|MCP_NETBOX_TOKEN=${NEW_TOKEN}|" "$ENV_FILE"
fi

# --------------------------------------------------------------------
# 5. Sobe a stack
# --------------------------------------------------------------------
log "5/5 Build + up da stack (isso pode levar alguns minutos na primeira vez)..."
(cd "$REPO_DIR/netbox-docker" && docker compose build --no-cache && docker compose up -d)

SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
cat <<EOF

==========================================================================
Instalação concluída!

NetBox:        http://${SERVER_IP:-SEU_SERVIDOR}:8000
Usuário:       $(grep '^SUPERUSER_NAME=' "$ENV_FILE" | cut -d= -f2)
Senha:         $(grep '^SUPERUSER_PASSWORD=' "$ENV_FILE" | cut -d= -f2)
API Token:     $(grep '^SUPERUSER_API_TOKEN=' "$ENV_FILE" | cut -d= -f2)

*** Anote a senha e o token acima agora — eles não aparecem de novo. ***

Pendências que exigem dado real do cliente (edite $ENV_FILE e reinicie
os containers depois com: cd $REPO_DIR/netbox-docker && docker compose up -d):
  - ZABBIX_HOST / ZABBIX_TOKEN  -> integração com Zabbix (seção 3 do README)
  - DIODE_TOKEN / plugins.py    -> só se for usar Diode (seção 2.3 do README)

O NetBox MCP Server (agente de IA) e o netbox-zabbix-sync já estão de pé,
mas o segundo só vai sincronizar de verdade depois que ZABBIX_HOST e
ZABBIX_TOKEN forem preenchidos.
==========================================================================
EOF
