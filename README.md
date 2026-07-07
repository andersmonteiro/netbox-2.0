# Projeto NetBox — Docker + Automação de Preenchimento + Zabbix + Agente de IA

Kit de deploy para NetBox via Docker (com suporte a plugins), mais um
conjunto de automações para reduzir cadastro manual e integrações com
Zabbix e com agentes de IA (via MCP).

Este projeto **não reinventa** o docker-compose oficial do NetBox — ele
estende o repositório oficial [`netbox-community/netbox-docker`](https://github.com/netbox-community/netbox-docker)
(branch `release`) via `docker-compose.override.yml`.

**Sobre a versão do NetBox:** a imagem é fixada em `v4.5.10` (não
`latest`) por causa de compatibilidade de plugin — ver a seção
"Versão do NetBox e compatibilidade de plugins" mais abaixo antes de
mudar isso.

Plugins incluídos: **netbox-topology-views** (mapa de topologia),
**netbox-qrcode** (QR Code pra etiqueta física de device/rack/cabo) e
**netbox_diode_plugin** (ingestão via Diode/Orb Agent).

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
│   └── discover_network.py         <- descoberta de rede (nmap, fallback)
├── zabbix-sync/
│   └── config.py                   <- config do netbox-zabbix-sync
└── orb-agent/
    └── agent.yaml.example          <- descoberta de rede oficial (via Diode)
```

Este é um **repositório template**: nada aqui tem dado de cliente
(senha, IP, planilha). O `netbox-docker` oficial **não fica versionado
aqui** — o `setup.sh` clona ele fresco a cada deploy, então você sempre
usa a última versão estável. Cada cliente tem seu próprio `.env`
(gerado localmente a partir de `.env.example`, nunca commitado — ver
`.gitignore`).

## 1. Subindo o NetBox (Docker, última versão estável, com plugins)

### Servidor novo, sem nada instalado (recomendado para clientes)

Um único comando: instala Docker + dependências (git, nmap, python3,
jq...), clona este template, sobe o `netbox-docker` oficial com o
overlay aplicado, gera senha/token do superusuário automaticamente,
**sobe também o Diode + já deixa o Orb Agent pronto** (seção 2.3 —
ligado por padrão em todo cliente novo, use ou não; desative com
`WITH_DIODE=false`) e deixa a stack no ar.

```bash
curl -fsSL https://raw.githubusercontent.com/andersmonteiro/netbox-2.0/main/bootstrap.sh | bash
```

No final ele imprime a URL, usuário, senha e token gerados — anote na
hora, não aparecem de novo.

**Senha do superusuário**: se você não tiver exportado
`SUPERUSER_PASSWORD` antes, o script gera uma senha aleatória, mostra
ela na tela e pergunta "Usar essa senha? (Y/n)". Dar Enter (ou "y")
aceita a gerada; "n" pede pra digitar uma senha sua (digitação oculta,
como em `sudo`). Funciona normalmente mesmo rodando via `curl | bash`.
Ela aparece de novo no resumo final da instalação.

**Usando sempre a mesma senha/token (ex: padrão da empresa)**: por
padrão a senha e os tokens são gerados aleatoriamente a cada instalação
— o repositório público nunca tem um valor fixo real. Se você quiser
pular a pergunta e usar sempre a
mesma credencial de produção em todos os clientes, exporte as
variáveis antes do curl (elas ficam só no seu terminal/onde você
guardar o comando, nunca no git):

```bash
export SUPERUSER_PASSWORD='sua-senha-de-verdade'
export SUPERUSER_API_TOKEN='seu-token-fixo-de-40-hex'   # ex: openssl rand -hex 20
export SUPERUSER_API_KEY='sua-key-fixa-de-32-hex'       # ex: openssl rand -hex 16
curl -fsSL https://raw.githubusercontent.com/andersmonteiro/netbox-2.0/main/bootstrap.sh | bash
```

Se alguma dessas variáveis não for definida, o bootstrap continua
gerando aleatoriamente só a que faltar.

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
`netbox-docker/.env` com os dados reais do cliente (gere
`SUPERUSER_API_TOKEN` e `SUPERUSER_API_KEY` com `openssl rand -hex 20`
e `openssl rand -hex 16`, por exemplo). Se preferir revisar antes de
subir, rode só `./setup.sh` (sem `--up`), edite o `.env` com calma, e
depois `cd netbox-docker && docker compose up -d`.

**Importante — `CSRF_TRUSTED_ORIGINS`**: também ajuste essa variável no
`.env` pro endereço real que você vai usar pra acessar o NetBox (ex:
`CSRF_TRUSTED_ORIGINS=http://192.168.1.10:8000`). O `bootstrap.sh`
(fluxo acima) faz isso sozinho detectando o IP do servidor; aqui, como
você está editando o `.env` na mão, precisa colocar você mesmo — sem
isso a tela de login trava com "403 — A verificação de CSRF falhou"
mesmo com a senha certa.

> **Não apague a linha `SKIP_SUPERUSER=false` do `.env`.** O
> `netbox-docker` vem com esse valor em `true` por padrão — se essa
> linha sumir do seu `.env`, o container sobe normalmente mas o
> usuário `admin` nunca é criado, e o login falha silenciosamente (a
> senha existe no arquivo, só que não foi usada por ninguém). Se isso
> acontecer: confira que `SKIP_SUPERUSER=false` e `SUPERUSER_API_KEY`
> estão no `.env`, depois `docker compose up -d --force-recreate
> netbox`.

Acesse `http://SEU_SERVIDOR:8000` depois de alguns minutos.

Isso já sobe, além do NetBox: Postgres, Redis, o worker, o
housekeeping, o **netbox-zabbix-sync** e o **netbox-mcp-server** — todos
definidos no `docker-compose.override.yml`.

Requisitos: Docker ≥ 20.10.10 e Docker Compose ≥ 1.28 no servidor
(qualquer Linux).

### Versão do NetBox e compatibilidade de plugins

A imagem base é `netboxcommunity/netbox:v4.5.10` (ver `Dockerfile-Plugins`),
**não** `latest`. Isso foi decisão consciente, não esquecimento:

- `netbox-topology-views` (mapa de topologia) hoje só declara suporte
  até NetBox 4.5.X. Não existe release compatível com a linha 4.6
  ainda — se a imagem usasse `latest` (hoje = v4.6.3), o build passa
  mas o plugin quebra/some da interface em runtime.
- `v4.5.10` (05/05/2026) foi o **último patch da série 4.5**. A série
  4.6 saiu no dia seguinte e já recebeu 3 patches próprios desde então
  (4.6.1, 4.6.2, 4.6.3) sem nenhum backport pra 4.5.x — é o padrão
  normal do NetBox: quando sai uma minor nova, o bugfix migra pra ela
  e a anterior para de receber patch. Na prática, a série 4.5 está
  congelada: não existe mais "ficar em 4.5.x e receber atualização
  automática" — por isso fixamos o número exato (`v4.5.10`) em vez da
  tag flutuante `v4.5`, pra deixar isso explícito.

**Risco que isso implica:** se aparecer uma falha de segurança na
série 4.5, não deve vir patch pra ela. Se isso acontecer, as opções
são: (a) confirmar se o `netbox-topology-views` já suporta 4.6 e fazer
o bump completo (tabela abaixo), (b) remover o `netbox-topology-views`
temporariamente e subir pra 4.6, ou (c) aceitar o risco por um tempo
controlado. Vale checar esporadicamente se saiu uma versão nova do
plugin, mesmo sem estar planejando upgrade.

Tabela de compatibilidade dos plugins deste template, hoje (NetBox 4.5.x):

| Plugin | Versão pinada | Compatível com 4.5.x | Compatível com 4.6.x |
| --- | --- | --- | --- |
| netbox_diode_plugin | 1.7.0 | Sim | Não (precisa 1.12.0) |
| netbox-topology-views | 4.5.1 | Sim | Não (sem release ainda) |
| netbox-qrcode | 0.0.20 | Sim | Não (precisa 0.0.21) |

**Como fazer upgrade pra NetBox 4.6+ no futuro:** confira se
`netbox-topology-views` já lançou versão pra 4.6 (é normalmente o
gargalo — os outros dois já têm release pronta pra 4.6). Se sim,
atualize os três pins em `plugin_requirements.txt` junto com o
`FROM netboxcommunity/netbox:v4.6.X` (use o número exato do patch mais
recente da série 4.6 nessa hora, pelo mesmo motivo acima) no
`Dockerfile-Plugins` **na mesma mudança**, e rode
`docker compose build --no-cache` num ambiente de teste antes de
aplicar em cliente. Nunca mude só a versão do NetBox sem checar essa
tabela primeiro.

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

**Opção oficial e mais robusta — Diode + Orb Agent (NetBox Labs):**
é o produto oficial da NetBox Labs pra isso, com reconciliação de
dados e suporte a múltiplos protocolos (SSH/NAPALM, SNMP, ping/porta).
Já deixei o plugin `netbox_diode_plugin` no `plugin_requirements.txt`
(pinado na versão 1.7.0, compatível com NetBox 4.5.x) pra você não
precisar rebuildar a imagem depois.

**Se você instalou via `bootstrap.sh` (padrão), os passos 1 e 2 abaixo
já rodaram sozinhos** — Diode sobe automaticamente (`WITH_DIODE=true`
é o default) e `orb-agent/agent.yaml` já sai criado com as credenciais
certas. Só falta o **passo 3**: editar os `targets` com as subnets
reais do cliente e rodar o container. Os passos 1-2 abaixo servem pra
quem instalou com `setup.sh` (sem bootstrap) ou desativou com
`WITH_DIODE=false`/`--no-diode` e quer ligar depois.

Pré-requisito: `jq` instalado (o `bootstrap.sh` já instala; se você usou
o `setup.sh` num servidor que já tinha Docker, confira com `jq --version`
e instale com `apt install jq` se faltar).

Passo 1 — subir o servidor Diode (stack própria, separada do NetBox;
pode rodar na mesma VM do NetBox, só que como um `docker compose`
diferente):

```bash
mkdir -p /opt/diode && cd /opt/diode
curl -sSfLo quickstart.sh https://raw.githubusercontent.com/netboxlabs/diode/release/diode-server/docker/scripts/quickstart.sh
chmod +x quickstart.sh
./quickstart.sh http://SEU_NETBOX_IP:8000
docker compose up -d
```

O `quickstart.sh` já faz tudo sozinho: baixa o `docker-compose.yaml` e
`nginx.conf` do Diode, gera **três** clients OAuth2 em
`oauth2/client/client-credentials.json` (`diode-ingest`,
`diode-to-netbox`, `netbox-to-diode`), preenche o `.env` com os
segredos e o `NETBOX_HOST`, e no final imprime o `DIODE_CLIENT_ID` /
`DIODE_CLIENT_SECRET` do client `diode-ingest` — já prontos pro Orb
Agent (passo 3). **Não precisa criar cliente manualmente** — isso é o
comportamento atual do script; se algum dia mudar, o próprio output
final dele avisa.

Passo 2 — pegar o secret do client `netbox-to-diode` (usado pelo plugin
dentro do NetBox, não pelo Orb Agent) e colar na configuração:

```bash
jq -r '.[] | select(.client_id=="netbox-to-diode") | .client_secret' \
  oauth2/client/client-credentials.json
```

Cole o valor em `configuration/plugins.py` →
`netbox_to_diode_client_secret`, e ajuste `diode_target_override` pra
`grpc://SEU_SERVIDOR_IP:8080/diode` (a porta é o `DIODE_NGINX_PORT` do
`.env` do Diode, 8080 por padrão). **Use o IP do servidor, não
`localhost`** — o Diode roda num `docker compose` separado do NetBox,
então "localhost" de dentro do container do NetBox não alcança o Diode
mesmo estando na mesma VM. Depois, rebuilde e reinicie o NetBox:

```bash
cd /opt/netbox-2.0/netbox-docker
docker compose build --no-cache
docker compose up -d
```

**Bug conhecido — 401 nos logs do NetBox ao consultar o Diode**: o
client `netbox-to-diode` criado pelo `quickstart.sh` só aceita
autenticação `client_secret_post`, mas o `netbox_diode_plugin` manda
`client_secret_basic` — o Hydra rejeita com 401 (`docker compose logs
netbox | grep -i diode` mostra `Diode Auth token introspection
failed`). O `bootstrap.sh` já corrige isso automaticamente; se você
seguiu esse passo 2 na mão, corrija recriando o client com o mesmo
secret via `authmanager` (que registra com o método certo):

```bash
cd /opt/diode
docker compose run --rm --no-deps diode-auth authmanager delete-client --client-id netbox-to-diode
docker compose run --rm --no-deps diode-auth authmanager create-client --client-id netbox-to-diode --scope "diode:read diode:write" --client-secret="O_MESMO_SECRET_DE_ANTES"
```

Passo 3 — configurar e rodar o Orb Agent. Copie
`orb-agent/agent.yaml.example` deste template para `orb-agent/agent.yaml`,
ajuste os `targets` (subnets reais do cliente), o `target:` do Diode
para `grpc://SEU_SERVIDOR_IP:8080/diode` (mesmo endereço do passo 2), e
o `client_id`/`client_secret` do client `diode-ingest` (pego com `jq`,
ver comentário no `.example`). **Cole o valor literal no YAML — o Orb
Agent não expande `${VAR}`** (testamos: ele manda a string
`"${DIODE_CLIENT_ID}"` literal pro Diode e a autenticação falha).
`agent.yaml` já está no `.gitignore` por conter esse secret. Depois:

```bash
cd orb-agent
docker run -d --name orb-agent --net=host --restart unless-stopped \
  -v "$(pwd)":/opt/orb/ \
  netboxlabs/orb-agent:latest run -c /opt/orb/agent.yaml
```

O `agent.yaml.example` já vem com uma policy de `network_discovery`
(varredura de subnet) e uma de `device_discovery` (SSH/NAPALM em
devices conhecidos, mesma ideia do `napalm_collect.py` mas passando
pela reconciliação do Diode). Use `dry_run: true` no bloco `diode:` se
quiser conferir o que seria enviado antes de aplicar de verdade no
NetBox — mais detalhes e outros backends (SNMP, jumphost/bastion) em
https://netboxlabs.com/docs/orb-agent/config_samples.

> Nota de licença: Diode e Orb Agent são distribuídos sob a "NetBox
> Limited Use License 1.0" (não é Apache/MIT) — gratuito para uso, mas
> vale ler os termos antes de colocar em produção. O Orb Agent está em
> estágio "Public Preview" (pode mudar).

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
git remote add origin https://github.com/andersmonteiro/netbox-2.0.git
git push -u origin main
```

Se você já criou o repositório como Private em alguma tentativa
anterior, dá pra mudar depois: Settings do repo → rolar até "Danger
Zone" → **Change visibility** → Public.

**Fluxo por cliente:**

1. No servidor do cliente (novo, sem nada instalado):
   `curl -fsSL https://raw.githubusercontent.com/andersmonteiro/netbox-2.0/main/bootstrap.sh | bash`
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
- [netbox-community/netbox-topology-views](https://github.com/netbox-community/netbox-topology-views) (tabela de compatibilidade no README)
- [netbox-community/netbox-qrcode](https://github.com/netbox-community/netbox-qrcode) (COMPATIBILITY.md)
- [Orb Agent — configuração e exemplos](https://netboxlabs.com/docs/orb-agent/config_samples)
- [netboxcommunity/netbox tags (Docker Hub)](https://hub.docker.com/r/netboxcommunity/netbox/tags) — usado para confirmar qual patch a tag `v4.5` aponta hoje
