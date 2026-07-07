# Multi-stage build: the final image contains the installed package only —
# no build tools, no repo files, and (by design) no config or data. Those
# are mounted at runtime: /config read-only, /data for the SQLite ledger,
# /logs for the JSON log mirror (see compose.yaml + docs/DEPLOYMENT.md).

FROM python:3.12-slim AS build

WORKDIR /src
COPY pyproject.toml README.md ./
COPY hrusha/ hrusha/
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.12-slim

RUN useradd --create-home --uid 10001 hrusha
COPY --from=build /install /usr/local

USER hrusha
ENV HRUSHA_CONFIG=/config/config.yaml
# one config.yaml works bare-metal AND mounted: the env override wins
ENV HRUSHA_DB_PATH=/data/hrusha.db
VOLUME ["/config", "/data", "/logs"]

ENTRYPOINT ["hrusha"]
# 0.0.0.0 is correct INSIDE the container — the host-side port binding in
# compose.yaml (default 127.0.0.1) is the actual exposure boundary
CMD ["serve", "--host", "0.0.0.0", "--port", "8787"]
