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
#   6. Sobe também o servidor Diode (stack separada) por padrão -- todo
#      cliente novo já sai com Diode + Orb Agent prontos pra usar, use
#      ou não. Preenche o plugins.py do NetBox com as credenciais
#      geradas e já deixa orb-agent/agent.yaml pronto (só falta editar
#      os "targets"), antes de buildar -- ver seção 2.3 do README.
#      Continua a instalação normalmente mesmo se isso falhar (não deve
#      travar o NetBox em si). Desative com WITH_DIODE=false / --no-diode.
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
#   WITH_DIODE          -> default: true. Diode já sobe junto por
#                          padrão em todo cliente novo. Defina "false"
#                          pra pular (equivalente à flag --no-diode
#                          abaixo -- use a variável quando rodar via
#                          "curl | bash", já que nesse modo passar flag
#                          pro script exige a sintaxe mais chata
#                          "curl ... | bash -s -- --no-diode").
#   DIODE_DIR           -> default: /opt/diode
#
# Flags (só funcionam rodando o arquivo direto, ex: ./bootstrap.sh
# --no-diode -- veja WITH_DIODE acima pro modo curl|bash):
#   --no-diode    desativa o Diode (equivale a WITH_DIODE=false)
#   --with-diode  força ligado (já é o padrão, existe só por clareza)
# ==========================================================================
set -euo pipefail

TEMPLATE_REPO_URL="${TEMPLATE_REPO_URL:-https://github.com/andersmonteiro/netbox-2.0.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/netbox-2.0}"
DIODE_DIR="${DIODE_DIR:-/opt/diode}"
WITH_DIODE="${WITH_DIODE:-true}"
for _arg in "$@"; do
    case "$_arg" in
        --with-diode) WITH_DIODE=true ;;
        --no-diode) WITH_DIODE=false ;;
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
    # depois, sem precisar voltar e instalar na mão. snmp (snmpget/
    # snmpwalk) é usado pelo automation-scripts/discovery_netbox.py.
    $SUDO apt-get install -y git curl ca-certificates gnupg nmap python3 python3-venv python3-pip openssl jq snmp
else
    warn "apt-get não encontrado. Instale manualmente: git curl nmap python3 python3-venv python3-pip openssl jq snmp"
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
# 5. Diode (ligado por padrão -- desative com WITH_DIODE=false ou --no-diode)
# --------------------------------------------------------------------
if [ "$WITH_DIODE" = "true" ]; then
    log "5/7 Subindo o Diode..."
    # Roda antes do build do NetBox de propósito: assim o plugins.py já
    # sai do build com as credenciais certas, sem precisar rebuildar
    # duas vezes. Se algo falhar aqui, avisamos e seguimos com o resto
    # da instalação normalmente -- Diode é opcional, não deve travar o
    # NetBox em si.
    if ! command -v jq >/dev/null 2>&1; then
        warn "jq não encontrado -- pulando setup automático do Diode. Instale jq e siga a seção 2.3 do README manualmente."
    else
        mkdir -p "$DIODE_DIR"
        DIODE_LOG="$DIODE_DIR/install.log"
        DIODE_OK=true
        (
            cd "$DIODE_DIR"
            if [ ! -f quickstart.sh ]; then
                curl -sSfLo quickstart.sh https://raw.githubusercontent.com/netboxlabs/diode/release/diode-server/docker/scripts/quickstart.sh
                chmod +x quickstart.sh
            fi
            # quickstart.sh + docker compose up são barulhentos (pull de
            # imagem, etc.) -- manda pra um log em vez de poluir a tela;
            # a mensagem final resume o que importa.
            ./quickstart.sh "http://${SERVER_IP:-localhost}:8000" > "$DIODE_LOG" 2>&1

            # Pin de versão: a branch "release" (que o quickstart.sh
            # sempre baixa, sem versão fixa) evolui sem aviso. Confirmado
            # em produção que a versão atual dela (2.0.0, 22/mai) exige o
            # plugin netbox_diode_plugin >=1.12.0, que por sua vez exige
            # NetBox >=4.6.0. Este template fixa NetBox em 4.5.x (por
            # causa do netbox-topology-views, que ainda não suporta 4.6 --
            # ver plugin_requirements.txt), então isso quebra a ingestão
            # de forma silenciosa: o diode-reconciler chama
            # /api/plugins/diode/bulk-plan-apply/, endpoint que só existe
            # a partir do plugin 1.12.0. Com o plugin 1.7.0 (o que está
            # pinado aqui), esse endpoint dá 404 -- login e ingestão no
            # Diode funcionam normalmente, mas nada cai no IPAM do NetBox.
            # A última leva de imagens anterior a essa mudança (13/jan,
            # release com "NetBox 4.5.x support") ainda usa
            # apply-change-set/generate-diff, compatível com plugin
            # 1.7.0-1.11.0 / NetBox 4.5.x -- mas os 3 serviços do Diode
            # NÃO compartilham numeração de versão: diode-ingester e
            # diode-reconciler pararam em 1.13.0 nessa leva, diode-auth
            # em 1.12.0 (confirmado nas tags do Docker Hub -- não existe
            # netboxlabs/diode-auth:1.13.0). Por isso um único DIODE_TAG
            # não pina os três; sobrescrevemos a imagem do diode-auth via
            # override em vez de usar DIODE_TAG global. Ver README seção
            # 2.3.
            if ! grep -q '^DIODE_TAG=' .env 2>/dev/null; then
                echo "DIODE_TAG=1.13.0" >> .env
            fi
            cat > docker-compose.override.yml <<'DIODEPIN'
services:
  diode-auth:
    image: netboxlabs/diode-auth:1.12.0
  diode-auth-bootstrap:
    image: netboxlabs/diode-auth:1.12.0
DIODEPIN

            docker compose up -d >> "$DIODE_LOG" 2>&1
        ) || DIODE_OK=false

        if [ "$DIODE_OK" = "true" ] && [ -f "$DIODE_DIR/oauth2/client/client-credentials.json" ]; then
            DIODE_PORT="$(grep -oP 'DIODE_NGINX_PORT=\K[0-9]+' "$DIODE_DIR/.env" 2>/dev/null || echo 8080)"
            DIODE_SECRET="$(jq -r '.[] | select(.client_id=="netbox-to-diode") | .client_secret' "$DIODE_DIR/oauth2/client/client-credentials.json" 2>/dev/null || echo "")"

            # Bug conhecido: o client "netbox-to-diode" criado pelo
            # quickstart.sh só aceita autenticação via "client_secret_post",
            # mas o netbox_diode_plugin manda "client_secret_basic" -- o
            # Hydra rejeita com 401 (confirmado em produção, ver logs do
            # Hydra). Clients criados via "authmanager create-client" (CLI)
            # aceitam client_secret_basic, então recriamos com o MESMO
            # secret pra corrigir sem precisar tocar no plugins.py de novo.
            if [ -n "$DIODE_SECRET" ]; then
                # "docker compose up -d" (chamado logo acima) retorna assim
                # que os containers INICIAM, não quando o Hydra termina de
                # subir de verdade -- rodar o authmanager imediatamente
                # depois pode cair numa corrida e falhar por conexão
                # recusada. Tenta com retry (até ~30s) em vez de desistir
                # na primeira falha.
                DIODE_AUTH_FIX_OK=false
                for _fix_try in 1 2 3 4 5 6 7 8 9 10; do
                    if (
                        cd "$DIODE_DIR"
                        docker compose run --rm --no-deps diode-auth authmanager delete-client --client-id netbox-to-diode
                        docker compose run --rm --no-deps diode-auth authmanager create-client --client-id netbox-to-diode --scope "diode:read diode:write" --client-secret="$DIODE_SECRET"
                    ) >> "$DIODE_LOG" 2>&1; then
                        DIODE_AUTH_FIX_OK=true
                        break
                    fi
                    sleep 3
                done
                if [ "$DIODE_AUTH_FIX_OK" != "true" ]; then
                    warn "Não consegui recriar o client netbox-to-diode com o método de auth correto (mesmo com retry) -- detalhes em $DIODE_LOG. Se o NetBox mostrar 401 nos logs ao consultar o Diode, rode manualmente (seção 2.3 do README): docker compose run --rm --no-deps diode-auth authmanager delete-client --client-id netbox-to-diode && docker compose run --rm --no-deps diode-auth authmanager create-client --client-id netbox-to-diode --scope \"diode:read diode:write\" --client-secret=\"\$SECRET\""
                fi
            fi

            PLUGINS_PY="$REPO_DIR/netbox-docker/configuration/plugins.py"
            if [ -n "$DIODE_SECRET" ] && [ -f "$PLUGINS_PY" ]; then
                sed -i "s|\"diode_target_override\": \"grpc://diode.local:8080/diode\",|\"diode_target_override\": \"grpc://${SERVER_IP:-localhost}:${DIODE_PORT}/diode\",|" "$PLUGINS_PY"
                sed -i "s|\"netbox_to_diode_client_secret\": \"PREENCHER_APOS_QUICKSTART_DIODE\",|\"netbox_to_diode_client_secret\": \"${DIODE_SECRET}\",|" "$PLUGINS_PY"
                echo "    Diode no ar e plugins.py já configurado (grpc://${SERVER_IP:-localhost}:${DIODE_PORT}/diode). Log: $DIODE_LOG"
                DIODE_INGEST_SECRET="$(jq -r '.[] | select(.client_id=="diode-ingest") | .client_secret' "$DIODE_DIR/oauth2/client/client-credentials.json" 2>/dev/null || echo "")"

                # Deixa o Orb Agent pronto pra usar -- só falta o operador
                # editar os "targets" (subnets do cliente) e iniciar o
                # container (não iniciamos sozinhos: escanear rede às
                # cegas com subnets de exemplo não serve pra nada).
                AGENT_YAML="$REPO_DIR/orb-agent/agent.yaml"
                AGENT_EXAMPLE="$REPO_DIR/orb-agent/agent.yaml.example"
                if [ -n "$DIODE_INGEST_SECRET" ] && [ -f "$AGENT_EXAMPLE" ] && [ ! -f "$AGENT_YAML" ]; then
                    sed -e "s|target: grpc://SEU_DIODE_HOST:8080/diode|target: grpc://${SERVER_IP:-localhost}:${DIODE_PORT}/diode|" \
                        -e "s|client_secret: SUBSTITUA_PELO_SECRET_REAL|client_secret: ${DIODE_INGEST_SECRET}|" \
                        "$AGENT_EXAMPLE" > "$AGENT_YAML"
                    echo "    $AGENT_YAML criado com as credenciais -- falta só editar os 'targets' (subnets reais do cliente) e rodar (seção 2.3 do README, passo 3)."
                fi
            else
                warn "Diode subiu mas não consegui extrair o secret/plugins.py automaticamente. Siga a seção 2.3 do README (passo 2) na mão."
            fi
        else
            warn "Falha ao subir o Diode automaticamente -- detalhes em $DIODE_LOG. Siga a seção 2.3 do README na mão, ou rode de novo depois: cd $DIODE_DIR && docker compose up -d"
        fi
    fi
else
    log "5/7 WITH_DIODE=false -- pulando Diode (ver seção 2.3 do README para subir depois, ou WITH_DIODE=true / --with-diode)."
fi

# --------------------------------------------------------------------
# 6. Build da imagem e subida da stack
# --------------------------------------------------------------------
# Build e "up" imprimem muita coisa (pull/build de camada por camada) --
# manda pra log em vez de poluir a tela; se falhar, aponta pro log em
# vez de sumir sem explicação (esses dois passos não são opcionais
# como o Diode, então um erro aqui deve parar o script mesmo).
NETBOX_BUILD_LOG="$REPO_DIR/netbox-docker/build.log"
NETBOX_UP_LOG="$REPO_DIR/netbox-docker/up.log"

log "6/7 Build da imagem (com plugins)... (log em $NETBOX_BUILD_LOG)"
(cd "$REPO_DIR/netbox-docker" && docker compose build --no-cache) > "$NETBOX_BUILD_LOG" 2>&1 \
    || { warn "Build da imagem falhou -- veja $NETBOX_BUILD_LOG"; exit 1; }

log "7/7 Subindo a stack (isso pode levar vários minutos na 1ª vez -- o NetBox roda uma leva grande de migrations antes de virar 'healthy'; o docker-compose.override.yml já ajusta o healthcheck pra dar tempo suficiente, então o compose espera sozinho, sem erro de dependência no meio)... (log em $NETBOX_UP_LOG)"
(cd "$REPO_DIR/netbox-docker" && docker compose up -d) > "$NETBOX_UP_LOG" 2>&1 \
    || { warn "Subida da stack falhou -- veja $NETBOX_UP_LOG"; exit 1; }
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
') >> "$NETBOX_UP_LOG" 2>&1; then
                FINAL_PASSWORD="$TYPED_PASSWORD"
                sed -i "s|^SUPERUSER_PASSWORD=.*|SUPERUSER_PASSWORD=${FINAL_PASSWORD}|" "$ENV_FILE"
                echo "    Senha trocada."
            else
                warn "Não consegui trocar a senha automaticamente -- detalhes em $NETBOX_UP_LOG. A senha gerada acima ainda funciona."
            fi
        fi
    fi
fi

DIODE_PENDENCIA_LINE=""
if [ "$WITH_DIODE" != "true" ]; then
    DIODE_PENDENCIA_LINE="  - Diode/Orb Agent            -> opcional, seção 2.3 do README"
fi

DIODE_SUMMARY=""
if [ "$WITH_DIODE" = "true" ] && [ -n "${DIODE_INGEST_SECRET:-}" ]; then
    DIODE_SUMMARY="
Diode já está no ar e $REPO_DIR/orb-agent/agent.yaml já foi criado com
as credenciais certas (client_id/client_secret/target). Falta só:
  1. Editar os 'targets' nele com as subnets/blocos reais do cliente
  2. Rodar (seção 2.3 do README, passo 3):
     cd $REPO_DIR/orb-agent && docker run -d --name orb-agent --net=host \\
       --restart unless-stopped -v \"\$(pwd)\":/opt/orb/ \\
       netboxlabs/orb-agent:latest run -c /opt/orb/agent.yaml
Não iniciamos o container sozinhos de propósito -- rodar com as
subnets de exemplo do .example não serve pra nada.
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
