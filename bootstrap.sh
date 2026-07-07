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
#   1. Instala dependências de sistema (git, curl, nmap, python3, openssl, jq)
#   2. Instala Docker Engine + Docker Compose plugin (script oficial da
#      Docker), se ainda não estiverem instalados
#   3. Clona este template (se ainda não estiver rodando de dentro dele)
#   4. Roda o setup.sh (clona o netbox-docker oficial + aplica o overlay)
#   5. Gera (ou pergunta) a senha/token do superusuário
#   6. Se WITH_DIODE=true (ou --with-diode): sobe também o servidor
#      Diode (stack separada) e já preenche o plugins.py do NetBox com
#      as credenciais geradas, antes de buildar -- ver seção 2.3 do
#      README. Continua a instalação normalmente mesmo se isso falhar
#      (é um recurso opcional, não trava o resto).
#   7. Builda a imagem e sobe a stack — a 1ª subida roda uma leva grande
#      de migrations e pode levar vários minutos; o healthcheck do
#      netbox (ver docker-compose.override.yml) foi ajustado com
#      tolerância extra pra isso, então o `docker compose up -d` espera
#      sozinho até ficar saudável, sem imprimir erro de dependência no
#      meio do caminho.
#
# Testado em Ubuntu/Debian. Em outras distros, os passos 1-2 podem
# precisar de ajuste manual (o script avisa e continua mesmo assim).
#
# Variáveis de ambiente opcionais:
#   TEMPLATE_REPO_URL   -> default: https://github.com/andersmonteiro/netbox-2.0.git
#   INSTALL_DIR         -> default: /opt/netbox-2.0
#   SUPERUSER_PASSWORD  -> default: gera senha aleatória. Exporte pra usar
#                          uma senha fixa (ex: padrão da empresa) sem
#                          nunca commitar ela no repositório público.
#   SUPERUSER_API_TOKEN -> default: gera token aleatório (hex 40 chars).
#   SUPERUSER_API_KEY   -> default: gera key aleatória (hex 32 chars).
#   WITH_DIODE          -> default: false. Defina "true" pra já subir o
#                          Diode junto (equivalente à flag --with-diode
#                          abaixo -- use a variável quando rodar via
#                          "curl | bash", já que nesse modo passar flag
#                          pro script exige a sintaxe mais chata
#                          "curl ... | bash -s -- --with-diode").
#   DIODE_DIR           -> default: /opt/diode
#
# Flag: --with-diode (só funciona rodando o arquivo direto, ex:
#   ./bootstrap.sh --with-diode -- veja WITH_DIODE acima pro modo curl|bash)
# ==========================================================================
set -euo pipefail

TEMPLATE_REPO_URL="${TEMPLATE_REPO_URL:-https://github.com/andersmonteiro/netbox-2.0.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/netbox-2.0}"
DIODE_DIR="${DIODE_DIR:-/opt/diode}"
WITH_DIODE="${WITH_DIODE:-false}"
for _arg in "$@"; do
    case "$_arg" in
        --with-diode) WITH_DIODE=true ;;
    esac
done

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
log "1/6 Detectando sistema e instalando dependências básicas..."
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
    # jq é usado pelo quickstart.sh do Diode (ver seção 2.3 do README) --
    # instalado aqui de cara pra já estar pronto se você for usar Diode
    # depois, sem precisar voltar e instalar na mão.
    $SUDO apt-get install -y git curl ca-certificates gnupg nmap python3 python3-venv python3-pip openssl jq
else
    warn "apt-get não encontrado. Instale manualmente: git curl nmap python3 python3-venv python3-pip openssl jq"
fi

# --------------------------------------------------------------------
# 2. Docker Engine + Compose plugin
# --------------------------------------------------------------------
log "2/6 Verificando Docker..."
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
log "3/6 Localizando/obtendo o template..."
# Quando rodado via "curl | bash" não existe arquivo de script real,
# então BASH_SOURCE[0] vem vazio -- com "set -u" isso quebraria sem o
# ":-" abaixo. Nesse caso SCRIPT_DIR cai pro diretório atual (ex:
# /root), que não tem setup.sh, então a lógica abaixo corretamente
# segue pro branch de clone/atualização em INSTALL_DIR.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd || pwd)"
if [ -f "$SCRIPT_DIR/setup.sh" ] && [ -f "$SCRIPT_DIR/docker-compose.override.yml" ]; then
    echo "    Já estamos dentro do template ($SCRIPT_DIR), usando esta pasta."
    REPO_DIR="$SCRIPT_DIR"
else
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "    $INSTALL_DIR já existe, sincronizando com o repositório remoto..."
        # Usamos fetch + reset --hard (não "git pull") de propósito: uma
        # rodada anterior pode ter dado chmod +x nos scripts, o que o
        # git enxerga como alteração local e bloquearia um merge normal.
        # Esta pasta é só um espelho do template -- customização de
        # cliente de verdade vive fora do git (.env, netbox-docker/,
        # que já estão no .gitignore e não são tocados por isso).
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
chmod +x setup.sh bootstrap.sh 2>/dev/null || true

# --------------------------------------------------------------------
# 4. setup.sh: clona netbox-docker oficial + aplica overlay + cria .env
# --------------------------------------------------------------------
log "4/6 Preparando a stack (netbox-docker oficial + overlay deste template)..."
./setup.sh

ENV_FILE="$REPO_DIR/netbox-docker/.env"

# --------------------------------------------------------------------
# CSRF_TRUSTED_ORIGINS: sem isso o Django barra a TELA DE LOGIN com
# "403 -- A verificação de CSRF falhou" antes mesmo de checar
# usuário/senha, quando o acesso é por IP (que é o caso normal aqui).
# Preenchemos com o IP detectado do servidor + localhost. Se o cliente
# depois passar a acessar por outro endereço/domínio, precisa adicionar
# na mão em $ENV_FILE (documentado no .env.example).
# --------------------------------------------------------------------
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -n "$SERVER_IP" ] && grep -q "^CSRF_TRUSTED_ORIGINS=http://localhost:8000$" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^CSRF_TRUSTED_ORIGINS=http://localhost:8000$|CSRF_TRUSTED_ORIGINS=http://${SERVER_IP}:8000 http://localhost:8000|" "$ENV_FILE"
fi

# --------------------------------------------------------------------
# Preenche senha/token/key do superusuário.
#
# Por padrão GERA VALORES ALEATÓRIOS (evita credencial previsível em
# produção -- este template é público no GitHub, então nunca colocamos
# um valor fixo real em .env.example).
#
# Se você quiser usar uma senha/token FIXOS (ex: padrão da sua empresa
# pra facilitar acesso em vários clientes), exporte as variáveis ANTES
# de rodar o curl -- elas nunca tocam o repositório, ficam só na sua
# sessão/onde você guardar o comando:
#
#   export SUPERUSER_PASSWORD='sua-senha-de-verdade'
#   export SUPERUSER_API_TOKEN='seu-token-fixo-de-40-hex'
#   export SUPERUSER_API_KEY='sua-key-fixa-de-32-hex'
#   curl -fsSL https://raw.githubusercontent.com/andersmonteiro/netbox-2.0/main/bootstrap.sh | bash
# --------------------------------------------------------------------
if grep -q "troque-esta-senha" "$ENV_FILE" 2>/dev/null; then
    if [ -n "${SUPERUSER_PASSWORD:-}" ]; then
        # Veio via variável de ambiente -- usa direto, sem perguntar nada.
        FINAL_PASSWORD="$SUPERUSER_PASSWORD"
        echo "    Usando SUPERUSER_PASSWORD fornecido via variável de ambiente."
    else
        # Gera uma senha aleatória, mostra ela, e pergunta se quer trocar
        # (em vez de pedir pra digitar uma senha às cegas). Lemos de
        # /dev/tty (o terminal do usuário) em vez do stdin do script,
        # porque em "curl | bash" o stdin já está ocupado com o conteúdo
        # baixado pelo curl. Se não houver terminal (ex: cron/CI), fica
        # com a senha aleatória mesmo, sem perguntar.
        FINAL_PASSWORD="$(openssl rand -base64 18 | tr -d '=+/')"
        if [ -r /dev/tty ]; then
            echo ""
            echo "    Senha gerada automaticamente: $FINAL_PASSWORD"
            read -r -p "    Usar essa senha? (Y/n): " KEEP_PW < /dev/tty || KEEP_PW=""
            if [ "$KEEP_PW" = "n" ] || [ "$KEEP_PW" = "N" ]; then
                read -r -s -p "    Digite a nova senha do superusuário: " TYPED_PASSWORD < /dev/tty || TYPED_PASSWORD=""
                echo ""
                if [ -n "$TYPED_PASSWORD" ]; then
                    FINAL_PASSWORD="$TYPED_PASSWORD"
                fi
            fi
        fi
    fi
    sed -i "s|SUPERUSER_PASSWORD=troque-esta-senha|SUPERUSER_PASSWORD=${FINAL_PASSWORD}|" "$ENV_FILE"
fi
if grep -q "troque-este-token-de-40-caracteres" "$ENV_FILE" 2>/dev/null; then
    if [ -n "${SUPERUSER_API_TOKEN:-}" ]; then
        FINAL_TOKEN="$SUPERUSER_API_TOKEN"
        echo "    Usando SUPERUSER_API_TOKEN fornecido via variável de ambiente."
    else
        FINAL_TOKEN="$(openssl rand -hex 20)"
    fi
    sed -i "s|SUPERUSER_API_TOKEN=troque-este-token-de-40-caracteres|SUPERUSER_API_TOKEN=${FINAL_TOKEN}|" "$ENV_FILE"
    sed -i "s|NETBOX_TOKEN=\${SUPERUSER_API_TOKEN}|NETBOX_TOKEN=${FINAL_TOKEN}|" "$ENV_FILE"
    sed -i "s|MCP_NETBOX_TOKEN=\${SUPERUSER_API_TOKEN}|MCP_NETBOX_TOKEN=${FINAL_TOKEN}|" "$ENV_FILE"
fi
# A partir do NetBox 4.3 (token de API "v2"), SUPERUSER_API_TOKEN sozinho
# não cria o token -- precisa também de SUPERUSER_API_KEY.
if grep -q "troque-esta-chave-de-32-caracteres" "$ENV_FILE" 2>/dev/null; then
    if [ -n "${SUPERUSER_API_KEY:-}" ]; then
        FINAL_API_KEY="$SUPERUSER_API_KEY"
        echo "    Usando SUPERUSER_API_KEY fornecido via variável de ambiente."
    else
        FINAL_API_KEY="$(openssl rand -hex 16)"
    fi
    sed -i "s|SUPERUSER_API_KEY=troque-esta-chave-de-32-caracteres|SUPERUSER_API_KEY=${FINAL_API_KEY}|" "$ENV_FILE"
fi

# --------------------------------------------------------------------
# 5. Diode (opcional -- WITH_DIODE=true ou --with-diode)
# --------------------------------------------------------------------
if [ "$WITH_DIODE" = "true" ]; then
    log "5/7 Subindo o Diode (WITH_DIODE=true)..."
    # Roda antes do build do NetBox de propósito: assim o plugins.py já
    # sai do build com as credenciais certas, sem precisar rebuildar
    # duas vezes. Se algo falhar aqui, avisamos e seguimos com o resto
    # da instalação normalmente -- Diode é opcional, não deve travar o
    # NetBox em si.
    if ! command -v jq >/dev/null 2>&1; then
        warn "jq não encontrado -- pulando setup automático do Diode. Instale jq e siga a seção 2.3 do README manualmente."
    else
        mkdir -p "$DIODE_DIR"
        DIODE_OK=true
        (
            cd "$DIODE_DIR"
            if [ ! -f quickstart.sh ]; then
                curl -sSfLo quickstart.sh https://raw.githubusercontent.com/netboxlabs/diode/release/diode-server/docker/scripts/quickstart.sh
                chmod +x quickstart.sh
            fi
            ./quickstart.sh "http://${SERVER_IP:-localhost}:8000"
            docker compose up -d
        ) || DIODE_OK=false

        if [ "$DIODE_OK" = "true" ] && [ -f "$DIODE_DIR/oauth2/client/client-credentials.json" ]; then
            DIODE_PORT="$(grep -oP 'DIODE_NGINX_PORT=\K[0-9]+' "$DIODE_DIR/.env" 2>/dev/null || echo 8080)"
            DIODE_SECRET="$(jq -r '.[] | select(.client_id=="netbox-to-diode") | .client_secret' "$DIODE_DIR/oauth2/client/client-credentials.json" 2>/dev/null || echo "")"
            PLUGINS_PY="$REPO_DIR/netbox-docker/configuration/plugins.py"
            if [ -n "$DIODE_SECRET" ] && [ -f "$PLUGINS_PY" ]; then
                sed -i "s|\"diode_target_override\": \"grpc://diode.local:8080/diode\",|\"diode_target_override\": \"grpc://${SERVER_IP:-localhost}:${DIODE_PORT}/diode\",|" "$PLUGINS_PY"
                sed -i "s|\"netbox_to_diode_client_secret\": \"PREENCHER_APOS_QUICKSTART_DIODE\",|\"netbox_to_diode_client_secret\": \"${DIODE_SECRET}\",|" "$PLUGINS_PY"
                echo "    Diode no ar e plugins.py já configurado (grpc://${SERVER_IP:-localhost}:${DIODE_PORT}/diode)."
                DIODE_INGEST_SECRET="$(jq -r '.[] | select(.client_id=="diode-ingest") | .client_secret' "$DIODE_DIR/oauth2/client/client-credentials.json" 2>/dev/null || echo "")"
            else
                warn "Diode subiu mas não consegui extrair o secret/plugins.py automaticamente. Siga a seção 2.3 do README (passo 2) na mão."
            fi
        else
            warn "Falha ao subir o Diode automaticamente. Siga a seção 2.3 do README na mão, ou rode de novo depois: cd $DIODE_DIR && docker compose up -d"
        fi
    fi
else
    log "5/7 WITH_DIODE não definido -- pulando Diode (opcional, ver seção 2.3 do README para subir depois)."
fi

# --------------------------------------------------------------------
# 6. Build da imagem e subida da stack
# --------------------------------------------------------------------
log "6/7 Build da imagem (com plugins)..."
(cd "$REPO_DIR/netbox-docker" && docker compose build --no-cache)

log "7/7 Subindo a stack (isso pode levar vários minutos na 1ª vez -- o NetBox roda uma leva grande de migrations antes de virar 'healthy'; o docker-compose.override.yml já ajusta o healthcheck pra dar tempo suficiente, então o compose espera sozinho, sem erro de dependência no meio)..."
(cd "$REPO_DIR/netbox-docker" && docker compose up -d)
echo "    Stack no ar."

DIODE_PENDENCIA_LINE=""
if [ "$WITH_DIODE" != "true" ]; then
    DIODE_PENDENCIA_LINE="  - Diode/Orb Agent            -> opcional, seção 2.3 do README"
fi

DIODE_SUMMARY=""
if [ "$WITH_DIODE" = "true" ] && [ -n "${DIODE_INGEST_SECRET:-}" ]; then
    DIODE_SUMMARY="
Diode já está no ar (seção 2.3 do README, passo 1-2 concluídos).
Falta só o passo 3 (Orb Agent), que precisa das subnets reais do
cliente pra escanear -- configure $DIODE_DIR/orb-agent/agent.yaml e use:
  DIODE_CLIENT_ID:     diode-ingest
  DIODE_CLIENT_SECRET: ${DIODE_INGEST_SECRET}
"
elif [ "$WITH_DIODE" = "true" ]; then
    DIODE_SUMMARY="
Diode foi solicitado (WITH_DIODE=true) mas algo falhou no setup
automático -- confira acima e siga a seção 2.3 do README na mão.
"
fi

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
${DIODE_PENDENCIA_LINE}
O NetBox MCP Server (agente de IA) e o netbox-zabbix-sync já estão de pé,
mas o segundo só vai sincronizar de verdade depois que ZABBIX_HOST e
ZABBIX_TOKEN forem preenchidos.
${DIODE_SUMMARY}==========================================================================
EOF
