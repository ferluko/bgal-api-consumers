# API Consumers (MVP) — UBI9 + Python 3.12, sin compilación. Sin git ni ssh: solo habla con Vault y con el API Subscriber.
# UID arbitrario de OpenShift (restricted-v2) y rootfs read-only: no escribe fuera de /tmp.
FROM registry.access.redhat.com/ubi9/python-312:latest

WORKDIR /opt/app-root/src
COPY --chown=1001:0 pyproject.toml README.md ./
COPY --chown=1001:0 src ./src
COPY --chown=1001:0 openapi ./openapi
RUN pip install --no-cache-dir . dumb-init==1.2.5.post1 && rm -rf ~/.cache

ENV PYTHONUNBUFFERED=1 HOME=/tmp
EXPOSE 8080
ENTRYPOINT ["/opt/app-root/bin/dumb-init", "--"]
CMD ["api-consumers"]
