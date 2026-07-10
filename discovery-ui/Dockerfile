# Build a partir da raiz do CONTEXTO DO COMPOSE (docker-compose.override.yml:
# build.context: . / build.dockerfile: discovery-ui/Dockerfile). Isso é
# netbox-docker/ (não a raiz deste template) -- o "docker compose" roda
# de dentro de netbox-docker/, e o setup.sh copia tanto automation-scripts/
# quanto discovery-ui/ pra lá antes do build, então os caminhos abaixo
# ("automation-scripts/discovery_core.py", "discovery-ui/app.py") batem
# com o que existe naquele contexto. discovery_core.py é compartilhado
# entre o CLI (discovery_netbox.py) e esta interface web, sem duplicar a
# lógica de coleta/aplicação.
FROM python:3.12-slim

# snmp: fornece snmpget/snmpwalk, usados por discovery_core.py pro
# caminho de descoberta via SNMP.
RUN apt-get update && apt-get install -y --no-install-recommends snmp \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY discovery-ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY automation-scripts/discovery_core.py .
# Templates TextFSM patched (ver _install_textfsm_overrides() em
# discovery_core.py) -- ficam em /app/textfsm_overrides, irmão de
# discovery_core.py, porque o código resolve o caminho relativo a
# __file__. Aplicados por cima dos templates do ntc-templates (pip) toda
# vez que discovery_core é importado, não só no build da imagem.
COPY automation-scripts/textfsm_overrides ./textfsm_overrides
# Scripts de migração one-off -- copiados igual discovery_core.py
# (achatados direto em /app, não dentro de automation-scripts/) pra
# rodar como "docker compose exec netbox-oracle python <script>.py".
# encrypt_existing_secrets.py: cifra discovery_password que ainda
# estiver em texto puro (ver encrypt_secret()/decrypt_secret() em
# discovery_core.py). backfill_connectivity_status.py: testa SSH/SNMP
# em devices com credencial configurada mas nunca testados (ver
# test_ssh_connectivity()/test_snmp_connectivity() em discovery_core.py).
COPY automation-scripts/encrypt_existing_secrets.py .
COPY automation-scripts/backfill_connectivity_status.py .
COPY discovery-ui/app.py .
COPY discovery-ui/templates ./templates
COPY discovery-ui/static ./static

RUN mkdir -p discovery_output/applied discovery_output/discarded

EXPOSE 5050

CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--timeout", "120", "app:app"]
