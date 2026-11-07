FROM alpine:3.22
RUN apk add --no-cache stunnel
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
