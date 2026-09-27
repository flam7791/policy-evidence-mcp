# Runs the server over Streamable HTTP, for use by MCP clients that connect by URL.
# Build:  docker build -t policy-evidence-mcp .
# Run:    docker run --rm -p 127.0.0.1:8000:8000 policy-evidence-mcp
# The server has no authentication, so publish the port on 127.0.0.1 only.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EVIDENCE_MCP_CACHE_DIR=/tmp/evidence-mcp-cache \
    EVIDENCE_MCP_INDEX_PATH=/app/index/index.json

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# Build a keyword index at image build time from the (fictional) sample corpus. Mount your own
# index at /app/index to serve real documents. For hybrid search, set EVIDENCE_MCP_EMBEDDINGS_URL
# and re-run `evidence-mcp ingest --corpus sample_corpus --embeddings` when the container starts
# (the platform's compose file does this), so vectors come from your embedding service.
COPY sample_corpus ./sample_corpus
RUN evidence-mcp ingest --corpus sample_corpus

# Run as an unprivileged user, who may rebuild the index.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app/index
USER appuser

EXPOSE 8000
CMD ["evidence-mcp", "serve", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
