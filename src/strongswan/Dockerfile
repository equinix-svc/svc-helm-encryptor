FROM alpine:3.22
RUN apk add --no-cache strongswan iproute2 iptables
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
