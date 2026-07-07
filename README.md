# Projeto NetBox — Docker + Automação de Preenchimento + Zabbix + Agente de IA

Kit de deploy para NetBox via Docker (com suporte a plugins), mais um
conjunto de automações para reduzir cadastro manual e integrações com
Zabbix e com agentes de IA (via MCP).

Este projeto **não reinventa** o docker-compose oficial do NetBox — ele
estende o repositório oficial [`netbox-community/netbox-docker`](https://github.com/netbox-community/netbox-docker)
(branch `release`, sempre na última versão estável compatível com
plugins) via `docker-compose.override.yml`. Assim você continua
recebendo atualizações de segurança com um simples `git pull`.

## Estrutura deste repositório

```
netbox-2.0/
├── README.md                      <- este arquivo
├── bootstrap.sh                    <- instalação 1-comando em servidor novo (Docker + tudo)
├── setup.sh                        <- só a parte de clonar netbox-docker + aplicar overlay
├── .gitignore                      <- não deixa segredo/dado de cliente ir pro git
├── .env.example                   <- copie para .env e preencha (por cliente)
├── docker-compose.override.yml    <- overlay sobre o netbox-docker oficial
├── Dockerfile-Plugins              <- build da imagem com plugins
├── plugin_requirements.txt         <- lista de plugins (pip)
├── configuration/
│   └── plugins.py                  <- plugins habilitados no NetBox
├── automation-scripts/             <- scripts pynetbox/napalm/nmap
│   ├── requirements.txt
│   ├── import_csv_to_netbox.py     <- importação em massa (CSV/XLSX)
│   ├── napalm_collect.py           <- coleta via SSH/API (NAPALM)
│   └── discover_network.py         <- descoberta de rede (nmap)
└── zabbix-sync/
    └── config.py                   <- config do netbox-zabbix-sync
```

Este é um **repositório template**: nada aqui tem dado de cliente
(senha, IP, planilha). O `netbox-docker` oficial **não fica versionado
aqui** — o `setup.sh` clona ele fresco a cada deploy, então você sempre
usa a última versão estável. Cada cliente tem seu próprio `.env`
(gerado localmente a partir de `.env.example`, nunca commitado — ver
`.gitignore`).

## 1. Subindo o NetBox (Docker, última versão estável, com plugins)

### Servidor novo, sem nada instalado (recomendado para clientes)

Um único comando: instala Docker + dependências (git, nmap, python3...),
clona este template, sobe o `netbox-docker` oficial com o overlay
aplicado, gera senha/token do superusuário automaticamente e já deixa a
stack no ar.

```bash
curl -fsSL https://raw.githubusercontent.com/SEU_USUARIO/netbox-template/main/bootstrap.sh | bash
```

(troque `SEU_USUARIO` pelo dono real do repositório no GitHub — ver
seção 6). No final ele imprime a URL, usuário, senha e token gerados —
anote na hora, não aparecem de novo.

Esse comando só funciona porque o repositório é **público** (sem
segredo nenhum nele — ver seção 7); a VM do cliente não precisa de
nenhuma credencial de GitHub pra isso.

### Servidor que já tem Docker (ou você prefere rodar por partes)

Com o repositório já clonado no servidor:

```bash
./setup.sh --up
```

Isso: clona (ou atualiza) o `netbox-docker` oficial dentro de
`netbox-docker/`, copia os arquivos de customização deste template pra
dentro dele, cria o `.env` a partir do `.env.example` (se ainda não
existir) e sobe a stack com `docker compose build --no-cache && docker
compose up -d`.

**Antes de rodar com `--up` pela primeira vez**, edite
`netbox-docker/.env` com os dados reais do cliente (gere o
`SUPERUSER_API_TOKEN` com `openssl rand -hex 20`, por exemplo). Se
preferir revisar antes de subir, rode só `./setup.sh` (sem `--up`),
edite o `.env` com calma, e depois `cd netbox-docker && docker compose
up -d`.

Acesse `http://SEU_SERVIDOR:8000` depois de alguns minutos.

Isso já sobe, além do NetBox: Postgres, Redis, o worker, o
housekeeping, o **netbox-zabbix-sync** e o **netbox-mcp-server** — todos
definidos no `docker-compose.override.yml`.

Requisitos: Docker ≥ 20.10.10 e Docker Compose ≥ 1.28 no servidor
(qualquer Linux). A imagem `netboxcommunity/netbox:latest` usada como
base é reconstruída ~a cada 24h pela comunidade e sempre aponta para a
última versão estável do NetBox.

## 2. Automações de preenchimento

Você pediu três frentes — seguem as três, cada uma resolvendo um cenário
diferente. Elas não se excluem: normalmente se usa CSV para a carga
inicial, NAPALM para manter os devices já cadastrados atualizados, e
scan/Diode para achar o que ainda não está no NetBox.

### 2.1 Importação em massa via CSV/Excel

`automation-scripts/import_csv_to_netbox.py` lê uma planilha e faz
upsert (cria ou atualiza) de Sites, Devices ou IPs.

```bash
cd automation-scripts
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export NETBOX_URL=http://localhost:8000
export NETBOX_TOKEN=<seu token>

python import_csv_to_netbox.py sites   planilhas/sites.xlsx
python import_csv_to_netbox.py devices planilhas/devices.csv
python import_csv_to_netbox.py ips     planilhas/ip_addresses.csv
```

Colunas esperadas: veja o docstring no topo de cada função no script.
Ele foi feito para ser fácil de adaptar caso sua planilha use nomes de
coluna diferentes.

### 2.2 Coleta automática via SSH/API (NAPALM)

`automation-scripts/napalm_collect.py` conecta nos devices que **já
existem** no NetBox (usando o IP de gerência e a `platform` cadastrados)
e preenche automaticamente número de série e interfaces.

```bash
python napalm_collect.py --site "Matriz" --username admin --password 'senha'
```

Pré-requisito: o Device no NetBox precisa ter `platform` (driver NAPALM:
`ios`, `eos`, `junos`, `nxos_ssh`, `iosxr`...) e `primary_ip4`
preenchidos — normalmente você cadastra isso via CSV (passo 2.1) e este
script completa o resto.

### 2.3 Descoberta de rede

Duas opções, dependendo do quanto você quer investir:

**Opção simples — `discover_network.py` (incluso neste pacote):**
faz um ping sweep com `nmap` e cria IPs no NetBox com a tag
`auto-discovered` para revisão manual.

```bash
python discover_network.py 10.0.0.0/24 --site "Matriz"
```

**Opção oficial e mais robusta — Diode + orb-agent (NetBox Labs):**
é o produto oficial da NetBox Labs para isso, com reconciliação de
dados e suporte a múltiplos protocolos (SSH, SNMP, ICMP). Já deixei o
plugin `netbox_diode_plugin` no `plugin_requirements.txt` para você não
precisar rebuildar a imagem depois. Para subir o servidor Diode
(stack própria, separada do NetBox):

```bash
mkdir /opt/diode && cd /opt/diode
curl -sSfLo quickstart.sh https://raw.githubusercontent.com/netboxlabs/diode/release/diode-server/docker/scripts/quickstart.sh
chmod +x quickstart.sh
./quickstart.sh http://SEU_NETBOX:8000
docker compose up -d
```

O script gera as credenciais OAuth2; pegue o `client_secret` do cliente
`netbox-to-diode` em `oauth2/client/client-credentials.json` e cole em
`configuration/plugins.py` (`netbox_to_diode_client_secret`). Depois
instale o [orb-agent](https://github.com/netboxlabs/orb-agent) apontando
para esse Diode server para automatizar a descoberta contínua. Detalhes
completos: https://github.com/netboxlabs/diode.

> Nota de licença: Diode é distribuído sob a "NetBox Limited Use
> License 1.0" (não é Apache/MIT) — gratuito para uso, mas vale ler os
> termos antes de colocar em produção.

## 3. Integração com Zabbix

Optei por **NetBox → Zabbix** (NetBox como fonte da verdade / CMDB,
alimentando o Zabbix) usando o
[netbox-zabbix-sync](https://github.com/TheNetworkGuy/netbox-zabbix-sync),
que é o projeto community mais maduro para isso. É a direção mais comum
porque evita conflito de "quem manda" nos dados — mas se você preferir o
sentido inverso (Zabbix descobre e alimenta o NetBox) ou bidirecional,
me avise que eu ajusto (as alternativas `SUSE/zabbix-netbox-sync` e
`OpensourceICTSolutions/nbxsync` cobrem esses outros cenários).

Já está definido como serviço no `docker-compose.override.yml`
(container `netbox-zabbix-sync`, roda a cada 5 minutos). Falta você:

1. Preencher `ZABBIX_HOST` e `ZABBIX_TOKEN` no `.env`.
2. Criar dois Custom Fields no NetBox (Admin > Custom Fields):
   - `zabbix_hostid` (Integer, objeto: dcim > device)
   - `zabbix_template` (Text, objeto: dcim > device_type) — só
     necessário se você **não** usar Config Context para templates
     (ver `zabbix-sync/config.py`, `templates_config_context`).
3. Garantir que o usuário do Zabbix usado no token tenha permissão de
   criar/editar/apagar hosts e hostgroups.
4. Ajustar `zabbix-sync/config.py` conforme sua topologia (formato dos
   hostgroups, mapeamento de inventário, etc. — comentado no arquivo).

## 4. Agente de IA via NetBox MCP Server

Você mencionou querer expor o NetBox para agentes de IA. O caminho mais
direto e oficial é o [NetBox MCP Server](https://github.com/netboxlabs/netbox-mcp-server)
(da própria NetBox Labs): um servidor MCP **somente leitura** por padrão
(consulta devices, IPs, changelog, faz busca) — dá para conversar em
linguagem natural com o inventário sem risco de um agente "quebrar" algo
sem querer.

Já está no `docker-compose.override.yml` (container
`netbox-mcp-server`, HTTP na porta `8001`). Para conectar o Claude
Desktop/Code a ele:

```bash
claude mcp add --transport http netbox http://SEU_SERVIDOR:8001/mcp
```

Ou no `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "netbox": {
      "url": "http://SEU_SERVIDOR:8001/mcp"
    }
  }
}
```

Depois disso dá pra perguntar coisas como "quais devices Cisco existem
no site Matriz?" ou "quem alterou o roteador core na última semana?"
diretamente no chat.

**Se você quiser que o agente também *escreva* no NetBox** (não só
consulte), há duas rotas, em ordem de recomendação:
1. Usar o Diode SDK (Python/Go) como camada de ingestão — ele já foi
   feito para isso, com reconciliação e auditoria.
2. Fazer fork do `netbox-mcp-server` (é Apache 2.0, o próprio projeto
   incentiva isso) e adicionar tools de escrita usando `pynetbox`.

Não recomendo dar um token de escrita direto a um MCP genérico sem
controle de escopo — prefira uma dessas duas opções para manter
auditoria do que foi alterado por automação vs. por humano.

## 5. Ordem sugerida de implementação

1. Suba o NetBox (seção 1) e crie o superusuário.
2. Cadastre a estrutura básica manualmente ou via CSV: Sites, Manufacturers,
   Device Types, Device Roles (seção 2.1).
3. Rode o NAPALM collector para enriquecer os devices já cadastrados
   (seção 2.2).
4. Configure o Zabbix sync (seção 3) para levar o inventário para
   monitoramento.
5. Conecte o MCP Server ao Claude (seção 4) para consultas em linguagem
   natural.
6. Se quiser descoberta contínua de rede, avalie subir o Diode +
   orb-agent (seção 2.3) depois que o básico estiver rodando.

## 6. Uso com GitHub (template público + múltiplos clientes)

Este repositório é **público** de propósito: é só ferramenta/automação
genérica, sem dado de cliente (o `.gitignore` bloqueia `.env`,
planilhas, credenciais do Diode etc. — ver seção 7). Isso permite que
qualquer servidor de cliente rode o `bootstrap.sh` via `curl | bash`
(seção 1) sem precisar de chave SSH nem token de acesso.

**Criar/configurar o repositório no GitHub:**

Na tela de criação, deixe **Visibility = Public**, desligue o toggle
"Add README" (a pasta local já tem um), e não adicione `.gitignore`
nem license por lá. Depois, localmente:

```bash
cd D:\projetos-natverk\netbox-2.0
git init -b main
git add -A
git commit -m "Template inicial: NetBox + Docker + automações + Zabbix + MCP"
git remote add origin https://github.com/SEU_USUARIO/netbox-template.git
git push -u origin main
```

Se você já criou o repositório como Private em alguma tentativa
anterior, dá pra mudar depois: Settings do repo → rolar até "Danger
Zone" → **Change visibility** → Public.

**Fluxo por cliente:**

1. No servidor do cliente (novo, sem nada instalado):
   `curl -fsSL https://raw.githubusercontent.com/SEU_USUARIO/netbox-template/main/bootstrap.sh | bash`
   — isso já clona o template, instala Docker, sobe tudo.
2. Edite `netbox-2.0/netbox-docker/.env` (gerado automaticamente pelo
   bootstrap com senha/token aleatórios) com os dados reais desse
   cliente que faltam: `ZABBIX_HOST`, `ZABBIX_TOKEN`, etc. Esse arquivo
   nunca é commitado, fica só no servidor do cliente.
3. Se esse cliente precisar de alguma customização que não deveria ir
   pro template (ex: um plugin específico), ou você faz isso só
   localmente ali no servidor (sem commitar), ou mantém um fork do
   template pra esse cliente. Não recomendo commitar particularidades
   de cliente direto no `main` do template público.
4. Melhorias genéricas (novo script, ajuste no compose, plugin novo de
   uso geral) entram no `main` do template via commit/push normal, e os
   outros clientes recebem rodando `bootstrap.sh`/`setup.sh` de novo
   (faz `git pull` internamente).

## 7. Segurança — o que NUNCA deve ir para este repositório (é público!)

- `.env` (senhas, tokens de API do NetBox/Zabbix/MCP)
- Qualquer coisa em `zabbix-sync/config.py` com segredo hardcoded
  (prefira variável de ambiente)
- Planilhas de importação com IPs/hostnames reais de cliente
- `client-credentials.json` do Diode (contém `client_secret` OAuth2)
- Nomes de clientes, topologia de rede real, ou qualquer coisa que
  identifique um cliente específico — isso é template, não deploy

O `.gitignore` já cobre os itens óbvios, mas como o repo é público,
**revise sempre** antes de cada `git push` — `git status` e
`git diff --cached` são seus amigos. Se algo sensível for commitado por
engano, trocar de visibilidade não apaga o histórico: é preciso
reescrever o histórico (`git filter-repo` ou similar) ou, no limite,
recriar o repositório.

## Fontes usadas na pesquisa deste projeto

- [netbox-community/netbox-docker](https://github.com/netbox-community/netbox-docker)
- [Using NetBox Plugins (wiki)](https://github.com/netbox-community/netbox-docker/wiki/Using-Netbox-Plugins)
- [TheNetworkGuy/netbox-zabbix-sync](https://github.com/TheNetworkGuy/netbox-zabbix-sync)
- [netboxlabs/netbox-mcp-server](https://github.com/netboxlabs/netbox-mcp-server)
- [netboxlabs/diode](https://github.com/netboxlabs/diode)
- [netboxlabs/diode-netbox-plugin](https://github.com/netboxlabs/diode-netbox-plugin)
