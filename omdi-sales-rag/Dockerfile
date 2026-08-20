FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY certs /tmp/omdi-extra-certs
RUN set -eu; \
    for certificate in /tmp/omdi-extra-certs/*.crt; do \
        [ -e "$certificate" ] || continue; \
        cp "$certificate" /usr/local/share/ca-certificates/; \
    done; \
    update-ca-certificates; \
    rm -rf /tmp/omdi-extra-certs

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

COPY scripts ./scripts
COPY sample_data ./sample_data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
