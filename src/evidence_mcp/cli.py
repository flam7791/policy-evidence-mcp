"""Command line: `evidence-mcp serve | ingest | search | eval`.

Logs always go to stderr. With the stdio transport, stdout carries the MCP protocol itself,
so a single stray print() to stdout would corrupt the connection with the client.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import CLASSIFICATION_LEVELS, Settings


def _serve(args: argparse.Namespace, settings: Settings) -> int:
    from .server import create_server

    server = create_server(settings)
    if args.transport == "stdio":
        server.run("stdio")
        return 0

    from mcp.server.transport_security import TransportSecuritySettings

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


def _ingest(args: argparse.Namespace, settings: Settings) -> int:
    from .retrieval import build_index, save_index

    ceiling = args.ceiling or settings.max_classification
    corpus = Path(args.corpus)
    if not corpus.is_dir():
        print(f"Corpus folder not found: {corpus}", file=sys.stderr)
        return 2
    index = build_index(corpus, ceiling, max_chars=args.max_chars)
    out = Path(args.out) if args.out else settings.index_path
    save_index(index, out)
    print(
        f"Indexed {len(index['documents'])} documents ({len(index['chunks'])} chunks) at ceiling "
        f"'{ceiling}'; skipped {index['excluded_documents']} above it. Wrote {out}",
        file=sys.stderr,
    )
    return 0


def _search(args: argparse.Namespace, settings: Settings) -> int:
    from .retrieval import load_index

    _, index = load_index(Path(args.index) if args.index else settings.index_path)
    for rank, hit in enumerate(
        index.search(args.query, args.top_k, settings.max_classification), 1
    ):
        print(f"{rank}. [{hit.score:.2f}] {hit.chunk.citation()}\n   {hit.chunk.text[:200]}...\n")
    return 0


def _eval(args: argparse.Namespace, settings: Settings) -> int:
    from .evaluation import evaluate, load_questions
    from .retrieval import load_index

    _, index = load_index(Path(args.index) if args.index else settings.index_path)
    report = evaluate(
        index, load_questions(Path(args.questions)), settings.max_classification, args.k
    )
    print(report.summary())
    for miss in report.misses:
        print(f"  miss: {miss}")
    ok = report.passed(args.min_hit)
    print("PASS" if ok else f"FAIL (need hit@{args.k} >= {args.min_hit} and zero leaks)")
    return 0 if ok else 1


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

    p = sub.add_parser("ingest", help="build the document index from a corpus folder")
    p.add_argument("--corpus", required=True)
    p.add_argument("--out", help="index path (default: EVIDENCE_MCP_INDEX_PATH)")
    p.add_argument("--ceiling", choices=CLASSIFICATION_LEVELS, help="highest level to index")
    p.add_argument("--max-chars", type=int, default=1200, help="maximum characters per chunk")

    p = sub.add_parser("search", help="search the document index from the terminal")
    p.add_argument("query")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--index")

    p = sub.add_parser("eval", help="measure retrieval quality against a question set")
    p.add_argument("--questions", required=True)
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--min-hit", type=float, default=0.8, help="minimum hit@k to pass")
    p.add_argument("--index")

    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # our own log line is enough
    settings = Settings.from_env()
    handlers = {"serve": _serve, "ingest": _ingest, "search": _search, "eval": _eval}
    return handlers[args.command](args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
