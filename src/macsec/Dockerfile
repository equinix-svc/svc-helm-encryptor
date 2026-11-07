FROM debian:sid-slim
LABEL org.opencontainers.image.source="https://github.com/equinix-svc/svc-helm-encryptor"
RUN apt-get update && apt-get install -y --no-install-recommends wpasupplicant iproute2 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
