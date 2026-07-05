# Multi-stage build: the final image contains the installed package only —
# no build tools, no repo files, and (by design) no config or data. Those
# are mounted at runtime: /config read-only, /data for the SQLite ledger.

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
VOLUME ["/config", "/data"]

ENTRYPOINT ["hrusha"]
# Placeholder until the Phase 5 service exists; overridden by compose.
CMD ["sync", "--dry-run"]
