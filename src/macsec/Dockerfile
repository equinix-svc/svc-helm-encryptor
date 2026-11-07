FROM debian:sid-slim
RUN apt-get update && apt-get install -y --no-install-recommends wpasupplicant iproute2 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
