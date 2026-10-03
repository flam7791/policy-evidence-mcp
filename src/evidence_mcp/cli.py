"""Command line: `evidence-mcp serve | ingest | search | eval`.

Logs always go to stderr. With the stdio transport, stdout carries the MCP protocol itself,
so a single stray print() to stdout would corrupt the connection with the client.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import CLASSIFICATION_LEVELS, Settings

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def build_auth(args: argparse.Namespace, public_url: str):
    """The token verifier and MCP AuthSettings for --auth, or (None, None)."""
    if args.auth == "none":
        return None, None
    from mcp.server.auth.settings import AuthSettings

    from .auth import (
        READ_SCOPE,
        EntraSettings,
        EntraTokenVerifier,
        StaticTokenVerifier,
        load_token_file,
    )

    if args.auth == "tokens":
        verifier = StaticTokenVerifier(load_token_file(Path(args.tokens_file)))
        issuer, scopes = public_url, [READ_SCOPE]
    else:
        entra = EntraSettings.from_env()
        verifier = EntraTokenVerifier(entra)
        issuer, scopes = entra.issuer, [READ_SCOPE]
    auth = AuthSettings(
        issuer_url=issuer,
        resource_server_url=public_url,
        required_scopes=scopes,
        validate_token_resource=False,  # the verifiers check the audience themselves
    )
    return verifier, auth


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    from . import tracing
    from .server import create_server

    tracing.configure("policy-evidence-mcp")
    if args.transport == "stdio":
        create_server(settings).run("stdio")
        return 0

    # An HTTP server reachable from other machines must authenticate its callers, unless the
    # operator states that the network itself is the boundary (a private container network).
    if args.auth == "none" and args.host not in LOOPBACK and not args.allow_unauthenticated:
        print(
            f"Refusing to serve without authentication on {args.host}. Use --auth tokens or "
            "--auth entra, or --allow-unauthenticated inside a private network.",
            file=sys.stderr,
        )
        return 2

    from mcp.server.transport_security import TransportSecuritySettings

    public_url = args.public_url or f"http://{args.host}:{args.port}/mcp"
    verifier, auth = build_auth(args, public_url)
    server = create_server(settings, token_verifier=verifier, auth=auth)

    # DNS-rebinding protection: a malicious web page could otherwise make the browser call a
    # server listening on localhost. Only requests addressed to these hosts are accepted,
    # even when the process binds 0.0.0.0 inside a container.
    hosts = ["127.0.0.1", "localhost", *args.allowed_host]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{h}:*" for h in hosts],
        allowed_origins=[f"http://{h}:*" for h in hosts],
    )
    server.run("streamable-http", host=args.host, port=args.port, transport_security=security)
    return 0


def _sync_sharepoint(args: argparse.Namespace, settings: Settings) -> int:
    from .sharepoint import GraphClient, SharePointConfig, SyncError, sync

    try:
        config = SharePointConfig.load(Path(args.config))
        report = sync(config, Path(args.out), GraphClient(config))
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"downloaded {report.downloaded}, unchanged {report.unchanged}, "
        f"by classification {report.by_classification}"
    )
    if report.unlabelled:
        print(
            f"{len(report.unlabelled)} file(s) without a mapped label, treated as "
            f"{config.unlabelled}: " + ", ".join(report.unlabelled[:10])
        )
    if report.skipped:
        print(f"skipped {len(report.skipped)}: " + ", ".join(report.skipped[:10]))
    print(f"next: evidence-mcp ingest --corpus {args.out}")
    return 0


def _token(args: argparse.Namespace, settings: Settings) -> int:
    from .auth import AuthConfigError, create_token, load_token_file, revoke_token

    path = Path(args.tokens_file)
    try:
        if args.action == "create":
            token = create_token(path, args.name, args.clearance)
            print(f"Token for {args.name} ({args.clearance}), shown once:\n{token}")
        elif args.action == "revoke":
            print("revoked" if revoke_token(path, args.name) else "no such token")
        else:
            for r in load_token_file(path):
                print(f"{r.name:<20} {r.clearance}")
    except AuthConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def _embedder(args: argparse.Namespace, settings: Settings):
    """The embedder for --embeddings: live, recorded to a cache file, or replayed offline."""
    from .embeddings import EmbeddingCache

    live = settings.embedder()
    cache = getattr(args, "embeddings_cache", None)
    if cache:
        return EmbeddingCache(Path(cache), live, offline=args.offline)
    if live is None or args.offline:
        raise SystemExit(
            "Semantic search needs EVIDENCE_MCP_EMBEDDINGS_URL (e.g. http://localhost:11434/v1), "
            "or --embeddings-cache FILE with recorded vectors."
        )
    return live


def _ingest(args: argparse.Namespace, settings: Settings) -> int:
    from .embeddings import EmbeddingError
    from .retrieval import build_index, save_index

    ceiling = args.ceiling or settings.max_classification
    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"Corpus folder not found: {corpus}", file=sys.stderr)
        return 2
    embedder = _embedder(args, settings) if args.embeddings else None
    try:
        index = build_index(corpus, ceiling, max_chars=args.max_chars, embedder=embedder)
    except EmbeddingError as exc:
        print(f"Embedding failed: {exc}", file=sys.stderr)
        return 3
    out = Path(args.out) if args.out else settings.index_path
    save_index(index, out)
    vectors = f", vectors from {index['embedding_model']}" if index.get("vectors") else ""
    print(
        f"Indexed {len(index['documents'])} documents ({len(index['chunks'])} chunks{vectors}) "
        f"at ceiling '{ceiling}'; skipped {index['excluded_documents']} above it. Wrote {out}",
        file=sys.stderr,
    )
    return 0


def _search(args: argparse.Namespace, settings: Settings) -> int:
    from .retrieval import load_index, searcher

    meta, keyword_index = load_index(Path(args.index) if args.index else settings.index_path)
    index = searcher(meta, keyword_index, settings.embedder())
    hits = index.search(args.query, args.top_k, settings.max_classification)
    print(f"Search mode: {getattr(index, 'last_mode', 'keywords')}\n")
    for rank, hit in enumerate(hits, 1):
        print(
            f"{rank}. [{hit.score:.2f}, {hit.matched_by}] {hit.chunk.citation()}\n"
            f"   {hit.chunk.text[:200]}...\n"
        )
    return 0


def _eval(args: argparse.Namespace, settings: Settings) -> int:
    from .evaluation import evaluate, load_questions
    from .retrieval import load_index, searcher

    meta, keyword_index = load_index(Path(args.index) if args.index else settings.index_path)
    questions = load_questions(Path(args.questions))
    modes = ["keywords", "hybrid"] if args.mode == "compare" else [args.mode]
    if "hybrid" in modes and not meta.get("vectors"):
        raise SystemExit("This index has no vectors: run `evidence-mcp ingest --embeddings`.")

    ok = True
    print(f"| Mode | hit@1 | hit@{args.k} | MRR | Leaks |\n|---|---|---|---|---|")
    reports = {}
    for mode in modes:
        index = (
            searcher(meta, keyword_index, _embedder(args, settings))
            if mode == "hybrid"
            else keyword_index
        )
        report = evaluate(index, questions, settings.max_classification, args.k)
        if getattr(index, "fallbacks", 0):
            raise SystemExit(f"Hybrid search fell back to keywords: {index.last_mode}")
        reports[mode] = report
        print(
            f"| {mode} | {report.hit_at_1:.2f} | {report.hit_at_k:.2f} | {report.mrr:.2f} | "
            f"{report.leaks} |"
        )
        ok = ok and report.passed(args.min_hit)
    for mode, report in reports.items():
        for miss in report.misses:
            print(f"  {mode} miss: {miss}")
    print("PASS" if ok else f"FAIL (need hit@{args.k} >= {args.min_hit} and zero leaks)")
    return 0 if ok else 1


def embedding_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--embeddings-cache", help="record vectors to / replay them from this JSON file")
    p.add_argument(
        "--offline", action="store_true", help="replay recorded vectors only; never call a model"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evidence-mcp", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the MCP server")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="extra host name clients may use to reach the HTTP server (repeatable)",
    )
    p.add_argument(
        "--auth",
        choices=["none", "tokens", "entra"],
        default="none",
        help="HTTP transport: bearer tokens from a token file, or Entra ID tokens",
    )
    p.add_argument(
        "--tokens-file", default=os.environ.get("EVIDENCE_MCP_TOKENS_FILE", "tokens.json")
    )
    p.add_argument("--public-url", help="the URL clients use, e.g. https://evidence.example/mcp")
    p.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help="serve without auth on a non-loopback address (private networks only)",
    )

    p = sub.add_parser(
        "sync-sharepoint", help="copy a SharePoint library into a corpus folder (Graph)"
    )
    p.add_argument("--config", required=True, help="sharepoint.toml")
    p.add_argument("--out", required=True, help="corpus folder to write")

    p = sub.add_parser("token", help="manage bearer tokens for --auth tokens")
    p.add_argument("action", choices=["create", "revoke", "list"])
    p.add_argument("--name")
    p.add_argument("--clearance", choices=CLASSIFICATION_LEVELS, default="public")
    p.add_argument(
        "--tokens-file", default=os.environ.get("EVIDENCE_MCP_TOKENS_FILE", "tokens.json")
    )

    p = sub.add_parser("ingest", help="build the document index from a corpus folder")
    p.add_argument("--corpus", required=True)
    p.add_argument("--out", help="index path (default: EVIDENCE_MCP_INDEX_PATH)")
    p.add_argument("--ceiling", choices=CLASSIFICATION_LEVELS, help="highest level to index")
    p.add_argument("--max-chars", type=int, default=1200, help="maximum characters per chunk")
    p.add_argument(
        "--embeddings", action="store_true", help="also store vectors, for hybrid search"
    )
    embedding_options(p)

    p = sub.add_parser("search", help="search the document index from the terminal")
    p.add_argument("query")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--index")

    p = sub.add_parser("eval", help="measure retrieval quality against a question set")
    p.add_argument("--questions", required=True)
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--min-hit", type=float, default=0.8, help="minimum hit@k to pass")
    p.add_argument("--index")
    p.add_argument("--mode", choices=["keywords", "hybrid", "compare"], default="keywords")
    embedding_options(p)

    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # our own log line is enough
    settings = Settings.from_env()
    if args.command == "token" and args.action != "list" and not args.name:
        parser.error("token create/revoke needs --name")
    handlers = {
        "serve": _serve,
        "ingest": _ingest,
        "search": _search,
        "eval": _eval,
        "token": _token,
        "sync-sharepoint": _sync_sharepoint,
    }
    return handlers[args.command](args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
