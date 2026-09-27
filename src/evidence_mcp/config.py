"""Runtime settings, read once from environment variables.

Every setting has a safe default, so the server runs with no configuration at all.
Override any of them with an environment variable prefixed EVIDENCE_MCP_ (for example in
the "env" block of an MCP client configuration).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Sensitivity levels, lowest to highest. A document is only indexed and returned when its
# level is at or below the configured ceiling (least privilege by default: "public").
CLASSIFICATION_LEVELS = ("public", "internal", "restricted")


@dataclass(frozen=True)
class Settings:
    sdmx_base_url: str = "https://sdmx.oecd.org/public/rest"
    cache_dir: Path = Path.home() / ".cache" / "evidence-mcp"
    index_path: Path = Path("index") / "index.json"
    max_classification: str = "public"
    # The OECD API allows about 60 requests per hour per client; stay below it.
    rate_limit_per_hour: int = 50
    http_timeout_seconds: float = 30.0
    # Semantic search (optional): an OpenAI-compatible /embeddings endpoint, e.g. Ollama at
    # http://localhost:11434/v1, an LLM gateway, or Azure OpenAI. Unset = keywords only.
    embeddings_url: str | None = None
    embeddings_model: str = "nomic-embed-text"
    embeddings_api_key: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        defaults = cls()
        settings = cls(
            sdmx_base_url=env.get("EVIDENCE_MCP_SDMX_BASE_URL", defaults.sdmx_base_url).rstrip("/"),
            cache_dir=Path(env.get("EVIDENCE_MCP_CACHE_DIR", str(defaults.cache_dir))),
            index_path=Path(env.get("EVIDENCE_MCP_INDEX_PATH", str(defaults.index_path))),
            max_classification=env.get(
                "EVIDENCE_MCP_MAX_CLASSIFICATION", defaults.max_classification
            ).lower(),
            rate_limit_per_hour=int(
                env.get("EVIDENCE_MCP_RATE_LIMIT_PER_HOUR", defaults.rate_limit_per_hour)
            ),
            http_timeout_seconds=float(
                env.get("EVIDENCE_MCP_HTTP_TIMEOUT", defaults.http_timeout_seconds)
            ),
            embeddings_url=env.get("EVIDENCE_MCP_EMBEDDINGS_URL") or None,
            embeddings_model=env.get("EVIDENCE_MCP_EMBEDDINGS_MODEL", defaults.embeddings_model),
            embeddings_api_key=env.get("EVIDENCE_MCP_EMBEDDINGS_API_KEY") or None,
        )
        settings.validate()
        return settings

    def embedder(self):
        """The configured embedding client, or None for keyword-only search."""
        if not self.embeddings_url:
            return None
        from .embeddings import OpenAIEmbeddings

        return OpenAIEmbeddings(
            self.embeddings_url, self.embeddings_model, api_key=self.embeddings_api_key
        )

    def validate(self) -> None:
        if self.max_classification not in CLASSIFICATION_LEVELS:
            raise ValueError(
                f"EVIDENCE_MCP_MAX_CLASSIFICATION must be one of {CLASSIFICATION_LEVELS}, "
                f"got {self.max_classification!r}"
            )
        if not self.sdmx_base_url.startswith("https://"):
            raise ValueError("EVIDENCE_MCP_SDMX_BASE_URL must use https://")
        if self.rate_limit_per_hour < 1:
            raise ValueError("EVIDENCE_MCP_RATE_LIMIT_PER_HOUR must be at least 1")


def classification_allowed(level: str, ceiling: str) -> bool:
    """True when a document at `level` may be shown under the `ceiling` clearance.

    Unknown levels are treated as the most sensitive, so a typo never leaks a document.
    """
    rank = {name: i for i, name in enumerate(CLASSIFICATION_LEVELS)}
    return rank.get(level.lower(), len(CLASSIFICATION_LEVELS)) <= rank[ceiling]
