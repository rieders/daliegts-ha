# Basis-Image fest eingetragen: Home Assistant würde sonst sein eigenes Alpine-Basisimage (ohne apt-get) einsetzen.
FROM docker.io/library/python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LAGERWERK_DATA_DIR=/data LAGERWERK_HA_INGRESS=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-liberation cups-client \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY src/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.lock
COPY src/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD ["/run.sh"]
