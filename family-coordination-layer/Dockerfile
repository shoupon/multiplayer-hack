FROM python:3.12-slim

ARG LITESTREAM_VERSION=0.3.13
ADD https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-v${LITESTREAM_VERSION}-linux-amd64.tar.gz /tmp/litestream.tgz
RUN tar -xzf /tmp/litestream.tgz -C /usr/local/bin litestream && rm /tmp/litestream.tgz

WORKDIR /app
COPY pyproject.toml README.md ./
COPY fcl ./fcl
RUN pip install --no-cache-dir . && useradd --create-home fcl && mkdir /data && chown fcl /data

COPY deploy/litestream.yml /etc/litestream.yml
COPY deploy/entrypoint.sh /usr/local/bin/fcl-entrypoint
USER fcl
ENV FCL_DB_PATH=/data/fcl.db FCL_HOST=0.0.0.0 FCL_PORT=8080
CMD ["/usr/local/bin/fcl-entrypoint"]
