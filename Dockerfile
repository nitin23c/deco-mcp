# syntax=docker/dockerfile:1
# deco-mcp container image: Python 3.14, the hashed dependency lock, no build tools.
FROM python:3.14-slim-trixie AS build
WORKDIR /src
COPY deploy/requirements.lock ./
RUN pip install --no-cache-dir --disable-pip-version-check \
      --require-hashes --only-binary=:all: --prefix=/install -r requirements.lock
# The package is pure Python: copy it in, no build backend needed.
COPY src/deco_mcp /install/lib/python3.14/site-packages/deco_mcp

FROM python:3.14-slim-trixie
LABEL org.opencontainers.image.title="deco-mcp" \
      org.opencontainers.image.description="MCP server for TP-Link Deco mesh routers" \
      org.opencontainers.image.source="https://github.com/nitin23c/deco-mcp" \
      org.opencontainers.image.licenses="MIT"
COPY --from=build /install /usr/local
RUN python -m compileall -q /usr/local/lib/python3.14/site-packages/deco_mcp \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin deco
ENV MCP_BIND=0.0.0.0 \
    MCP_PORT=8080 \
    PYTHONUNBUFFERED=1
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD ["python", "-I", "-m", "deco_mcp", "--healthcheck"]
ENTRYPOINT ["python", "-I", "-m", "deco_mcp"]
