FROM alpine:3.22
LABEL org.opencontainers.image.source="https://github.com/equinix-svc/svc-helm-encryptor"
RUN apk add --no-cache strongswan iproute2 iptables
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
