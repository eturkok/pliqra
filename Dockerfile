FROM python:3.13-slim

ARG VERSION=2.2.0
ARG REVISION=unknown
LABEL org.opencontainers.image.title="Pliqra" \
    org.opencontainers.image.description="Endpoint-first AI services, governed gateways and explicit model contracts" \
    org.opencontainers.image.source="https://github.com/eturkok/pliqra" \
    org.opencontainers.image.licenses="Apache-2.0" \
    org.opencontainers.image.version=$VERSION \
    org.opencontainers.image.revision=$REVISION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLIQRA_DATA_DIR=/data \
    PLIQRA_HOST=0.0.0.0 \
    PLIQRA_PORT=8080

WORKDIR /app
COPY LICENSE NOTICE ./
COPY requirements.lock .
RUN pip install --no-cache-dir -r requirements.lock \
    && groupadd --gid 10001 pliqra \
    && useradd --uid 10001 --gid pliqra --no-create-home pliqra \
    && mkdir -p /data \
    && chown pliqra:pliqra /data

COPY --chown=pliqra:pliqra pliqra ./pliqra
COPY --chown=pliqra:pliqra sdk ./sdk
COPY --chown=pliqra:pliqra docs ./docs
COPY --chown=pliqra:pliqra examples ./examples
USER pliqra:pliqra
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request,ssl; tls=os.getenv('PLIQRA_TLS_CERTFILE'); urllib.request.urlopen(('https' if tls else 'http')+'://127.0.0.1:'+os.getenv('PLIQRA_PORT','8080')+'/healthz',context=ssl._create_unverified_context() if tls else None,timeout=3)"
CMD ["python", "-m", "pliqra"]
