FROM python:3.12-slim
RUN pip install --no-cache-dir 'cryptography>=43,<50'
COPY agent.py /app/agent.py
USER 10001
ENTRYPOINT ["python3", "-u", "/app/agent.py"]
CMD ["run", "-c", "/etc/qkd/config.json"]
