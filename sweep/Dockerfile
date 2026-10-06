# Operator sweep tool. Build from plugins/ton/:  podman build -f sweep/Dockerfile -t bitcart-ton-sweep .
FROM python:3.12-slim
COPY sweep/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt && useradd --create-home --uid 1000 sweep \
    && mkdir /config && chown sweep /config
COPY sweep/sweep.py daemon/tonaddr.py /app/
USER sweep
WORKDIR /config
ENTRYPOINT ["python", "/app/sweep.py"]
