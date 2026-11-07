FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/equinix-svc/svc-helm-encryptor"
RUN pip install --no-cache-dir 'cryptography>=43,<50'
COPY exchange.py /app/exchange.py
USER 10001
ENTRYPOINT ["python3", "-u", "/app/exchange.py"]
CMD ["run", "-c", "/etc/qkd/config.json"]
