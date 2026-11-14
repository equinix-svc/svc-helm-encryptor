FROM alpine:3.22
LABEL org.opencontainers.image.source="https://github.com/equinix-svc/svc-helm-encryptor"
RUN apk add --no-cache iproute2
COPY apply.sh /usr/local/bin/apply.sh
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
