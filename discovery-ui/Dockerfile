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
COPY discovery-ui/app.py .
COPY discovery-ui/templates ./templates

RUN mkdir -p discovery_output/applied discovery_output/discarded

EXPOSE 5050

CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--timeout", "120", "app:app"]
