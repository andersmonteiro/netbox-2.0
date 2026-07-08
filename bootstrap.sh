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
#   5. Gera (ou pergunta) a senha/token do superusuário, restaura o
#      catálogo de device types (netbox-seed/) -- SÓ na 1ª instalação,
#      quando o banco ainda está vazio (ver seção 2.5 do README). Numa
#      reinstalação sobre um banco que já tem dado, isso é pulado
#      automaticamente pra não sobrescrever nada.
#   6. Builda a imagem e sobe a stack (inclui a discovery-ui, interface
#      web de descoberta em http://SEU_SERVIDOR:5050 -- login simples,
#      pensada pro time comercial revisar/aprovar descobertas sem usar
#      terminal) — a 1ª subida roda uma leva grande de migrations e
#      pode levar vários minutos; o healthcheck do netbox (ver
#      docker-compose.override.yml) foi ajustado com tolerância extra
#      pra isso, então o `docker compose up -d` espera sozinho até
#      ficar saudável, sem imprimir erro de dependência no meio do
#      caminho.
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
#   DISCOVERY_UI_PASSWORD -> default: gera senha aleatória. Senha de
#                          login da interface web de descoberta
#                          (discovery-ui, porta 5050) -- usuário fixo
#                          "admin" (troque DISCOVERY_UI_USER no .env se
#                          quiser outro).
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
log "1/7 Dependências do sistema..."
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
    # snmp (snmpget/snmpwalk) é usado pelo automation-scripts/discovery_netbox.py.
    $SUDO apt-get install -y git curl ca-certificates gnupg nmap python3 python3-venv python3-pip openssl snmp
else
    warn "apt-get não encontrado. Instale manualmente: git curl nmap python3 python3-venv python3-pip openssl snmp"
fi

# --------------------------------------------------------------------
# 2. Docker Engine + Compose plugin
# --------------------------------------------------------------------
log "2/7 Verificando Docker..."
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
log "3/7 Localizando/obtendo o template..."
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
log "4/7 Preparando a stack..."
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
SUPERUSER_PASSWORD_AUTO=false
if grep -q "troque-esta-senha" "$ENV_FILE" 2>/dev/null; then
    if [ -n "${SUPERUSER_PASSWORD:-}" ]; then
        # Veio via variável de ambiente -- usa direto, sem perguntar nada.
        FINAL_PASSWORD="$SUPERUSER_PASSWORD"
        echo "    Usando SUPERUSER_PASSWORD fornecido via variável de ambiente."
    else
        # Gera uma senha aleatória e segue direto, SEM parar o script pra
        # perguntar aqui -- isso travava o terminal bem no meio da
        # instalação, antes de Diode/build/up rodarem. A chance de trocar
        # essa senha fica pro final, depois que a stack inteira já
        # estiver de pé (ver bloco logo após "Stack no ar.").
        FINAL_PASSWORD="$(openssl rand -base64 18 | tr -d '=+/')"
        SUPERUSER_PASSWORD_AUTO=true
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
    sed -i "s|DISCOVERY_UI_NETBOX_TOKEN=\${SUPERUSER_API_TOKEN}|DISCOVERY_UI_NETBOX_TOKEN=${FINAL_TOKEN}|" "$ENV_FILE"
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
# Credenciais da discovery-ui (interface web de descoberta) -- login
# simples, não integrado ao NetBox, pro time comercial revisar/aprovar
# descobertas sem precisar de terminal. Mesma lógica de geração
# automática das credenciais do superusuário acima.
# --------------------------------------------------------------------
if grep -q "troque-esta-senha" "$ENV_FILE" 2>/dev/null; then
    if [ -n "${DISCOVERY_UI_PASSWORD:-}" ]; then
        FINAL_DISCOVERY_UI_PASSWORD="$DISCOVERY_UI_PASSWORD"
        echo "    Usando DISCOVERY_UI_PASSWORD fornecido via variável de ambiente."
    else
        FINAL_DISCOVERY_UI_PASSWORD="$(openssl rand -base64 18 | tr -d '=+/')"
    fi
    sed -i "s|DISCOVERY_UI_PASSWORD=troque-esta-senha|DISCOVERY_UI_PASSWORD=${FINAL_DISCOVERY_UI_PASSWORD}|" "$ENV_FILE"
fi
if grep -q "DISCOVERY_UI_SECRET_KEY=troque-esta-chave-aleatoria" "$ENV_FILE" 2>/dev/null; then
    FINAL_DISCOVERY_UI_SECRET_KEY="$(openssl rand -hex 24)"
    sed -i "s|DISCOVERY_UI_SECRET_KEY=troque-esta-chave-aleatoria|DISCOVERY_UI_SECRET_KEY=${FINAL_DISCOVERY_UI_SECRET_KEY}|" "$ENV_FILE"
fi

# --------------------------------------------------------------------
# 5. Catálogo de device types (netbox-seed/device-catalog.sql) -- SÓ
# roda na 1ª instalação, quando o banco Postgres ainda está vazio.
# O dump é um pg_dump COMPLETO (schema + dados), gerado a partir de uma
# instalação já catalogada com manufacturers/device types/templates de
# interface (sem nenhum device/IP real de cliente -- essas tabelas são
# tiradas do dump antes de chegar aqui, junto com usuário/sessão/audit
# log). Por isso subimos SÓ o Postgres primeiro: se a tabela
# django_migrations ainda não existir, é a 1ª subida e é seguro
# restaurar; se já existir, é uma reinstalação/atualização sobre um
# banco que já tem dado real, e não tocamos em nada.
# --------------------------------------------------------------------
log "5/7 Catálogo de device types..."
SEED_SQL="$REPO_DIR/netbox-seed/device-catalog.sql"
if [ ! -f "$SEED_SQL" ]; then
    echo "    netbox-seed/device-catalog.sql não encontrado, pulando."
else
    PG_ENV_FILE="$REPO_DIR/netbox-docker/env/postgres.env"
    PG_USER="$(grep '^POSTGRES_USER=' "$PG_ENV_FILE" 2>/dev/null | cut -d= -f2)"
    PG_DB="$(grep '^POSTGRES_DB=' "$PG_ENV_FILE" 2>/dev/null | cut -d= -f2)"
    PG_USER="${PG_USER:-netbox}"
    PG_DB="${PG_DB:-netbox}"

    (cd "$REPO_DIR/netbox-docker" && docker compose up -d postgres redis redis-cache < /dev/null) >/dev/null

    # Não confiamos em "pg_isready" sozinho pra decidir que já pode
    # consultar: no primeiro boot (volume vazio), o container do
    # postgres sobe uma instância TEMPORÁRIA só pra rodar scripts de
    # init, derruba e sobe de novo a instância de verdade -- pg_isready
    # pode responder "pronto" nessa instância temporária, antes do
    # socket real existir, dando um falso positivo. Em vez de checar
    # prontidão separado, tentamos a query de verdade em loop -- só
    # avança quando ela responder com sucesso.
    PG_READY=false
    DB_HAS_SCHEMA=""
    for _pg_try in $(seq 1 40); do
        if RAW_OUT="$(cd "$REPO_DIR/netbox-docker" && docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" -tAc "SELECT to_regclass('public.django_migrations');" < /dev/null 2>/dev/null)"; then
            DB_HAS_SCHEMA="$(echo "$RAW_OUT" | tr -d '[:space:]')"
            PG_READY=true
            break
        fi
        sleep 2
    done

    if [ "$PG_READY" != "true" ]; then
        warn "Postgres não respondeu a tempo -- pulando restauração do catálogo de device types. Rode manualmente depois (seção 2.5 do README) se quiser."
    else
        if [ -z "$DB_HAS_SCHEMA" ]; then
            echo "    Banco vazio (1ª instalação) -- restaurando netbox-seed/device-catalog.sql..."
            SEED_LOG="$REPO_DIR/netbox-docker/seed-restore.log"
            if (cd "$REPO_DIR/netbox-docker" && docker compose exec -T postgres psql -U "$PG_USER" -d "$PG_DB" < "$SEED_SQL") > "$SEED_LOG" 2>&1; then
                echo "    Catálogo restaurado: manufacturers, device types e templates de interface pré-cadastrados."
            else
                warn "Falha ao restaurar o catálogo de device types -- detalhes em $SEED_LOG. A instalação continua normalmente, só sem o catálogo pré-cadastrado."
            fi
        else
            echo "    Banco já tem schema (não é a 1ª instalação) -- não mexi em nada, catálogo não é reaplicado."
        fi
    fi
fi

# --------------------------------------------------------------------
# 6. Build da imagem e subida da stack
# --------------------------------------------------------------------
# Saída 100% nativa do Docker direto na tela (sem pipe/tee/redirect --
# qualquer um desses faz o Docker achar que não está num terminal de
# verdade e trocar a UI compacta com spinner/tempo por um log linha a
# linha). O healthcheck do netbox (docker-compose.override.yml, ~20min
# de tolerância) evita o erro de dependência "unhealthy" na 1ª subida,
# então "up -d" comum já espera sozinho.
# NETBOX_UP_LOG é usado só pela troca de senha (mais abaixo), sem
# relação com a saída do "up -d" em si.
NETBOX_UP_LOG="$REPO_DIR/netbox-docker/up.log"

log "6/7 Build da imagem (com plugins)..."
(cd "$REPO_DIR/netbox-docker" && docker compose --progress=tty build --no-cache < /dev/null)

log "7/7 Subindo a stack..."
(cd "$REPO_DIR/netbox-docker" && docker compose --progress=tty up -d < /dev/null)
echo "    Stack no ar."

# --------------------------------------------------------------------
# Chance de trocar a senha do superusuário -- só AGORA, com a stack
# inteira já de pé (não trava mais o script no meio da instalação).
# Só pergunta se a senha foi gerada automaticamente (se veio via
# SUPERUSER_PASSWORD por variável de ambiente, não há o que perguntar)
# e só se houver terminal interativo (/dev/tty); em "curl | bash" sem
# TTY (cron/CI) segue com a senha gerada, sem travar.
# --------------------------------------------------------------------
if [ "$SUPERUSER_PASSWORD_AUTO" = "true" ] && [ -r /dev/tty ]; then
    SUPERUSER_NAME_VAL="$(grep '^SUPERUSER_NAME=' "$ENV_FILE" | cut -d= -f2)"
    echo ""
    echo "    Senha gerada automaticamente: $FINAL_PASSWORD"
    read -r -p "    Usar essa senha? (Y/n): " KEEP_PW < /dev/tty || KEEP_PW=""
    if [ "$KEEP_PW" = "n" ] || [ "$KEEP_PW" = "N" ]; then
        read -r -s -p "    Digite a nova senha do superusuário: " TYPED_PASSWORD < /dev/tty || TYPED_PASSWORD=""
        echo ""
        if [ -n "$TYPED_PASSWORD" ]; then
            # A stack já está no ar -- o superusuário já foi criado no
            # boot anterior, então só editar o .env não muda mais nada;
            # troca a senha direto no container rodando. Secret vai por
            # variável de ambiente pro "docker compose exec" (não
            # interpolado no código Python), evitando problema de
            # aspas/caractere especial na senha digitada.
            if (cd "$REPO_DIR/netbox-docker" && docker compose exec -T \
                    -e NB_USER="$SUPERUSER_NAME_VAL" -e NEW_PW="$TYPED_PASSWORD" \
                    netbox /opt/netbox/netbox/manage.py shell -c '
import os
from django.contrib.auth import get_user_model
u = get_user_model().objects.get(username=os.environ["NB_USER"])
u.set_password(os.environ["NEW_PW"])
u.save()
' < /dev/null) >> "$NETBOX_UP_LOG" 2>&1; then
                FINAL_PASSWORD="$TYPED_PASSWORD"
                sed -i "s|^SUPERUSER_PASSWORD=.*|SUPERUSER_PASSWORD=${FINAL_PASSWORD}|" "$ENV_FILE"
                echo "    Senha trocada."
            else
                warn "Não consegui trocar a senha automaticamente -- detalhes em $NETBOX_UP_LOG. A senha gerada acima ainda funciona."
            fi
        fi
    fi
fi

cat <<EOF

==========================================================================
Instalação concluída!

NetBox:        http://${SERVER_IP:-SEU_SERVIDOR}:8000
Usuário:       $(grep '^SUPERUSER_NAME=' "$ENV_FILE" | cut -d= -f2)
Senha:         $(grep '^SUPERUSER_PASSWORD=' "$ENV_FILE" | cut -d= -f2)
API Token:     $(grep '^SUPERUSER_API_TOKEN=' "$ENV_FILE" | cut -d= -f2)

Descoberta de rede (interface web):
URL:           http://${SERVER_IP:-SEU_SERVIDOR}:5050
Usuário:       $(grep '^DISCOVERY_UI_USER=' "$ENV_FILE" | cut -d= -f2)
Senha:         $(grep '^DISCOVERY_UI_PASSWORD=' "$ENV_FILE" | cut -d= -f2)

*** Anote as senhas e o token acima agora — eles não aparecem de novo. ***
==========================================================================
EOF
