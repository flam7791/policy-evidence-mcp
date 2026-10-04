"""Access management: token checks, per-caller clearance, and the HTTP transport end to end."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from evidence_mcp import auth
from evidence_mcp.cli import main

# ------------------------------------------------------------------ static tokens


async def test_token_file_stores_hashes_only_and_verifies(tmp_path):
    path = tmp_path / "tokens.json"
    token = auth.create_token(path, "alice", "internal")
    stored = path.read_text()
    assert token not in stored and "alice" in stored
    verifier = auth.StaticTokenVerifier(auth.load_token_file(path))
    access = await verifier.verify_token(token)
    assert access.subject == "alice" and access.claims["clearance"] == "internal"
    assert await verifier.verify_token("emcp_wrong") is None


def test_duplicate_and_unknown_clearance_are_refused(tmp_path):
    path = tmp_path / "tokens.json"
    auth.create_token(path, "alice", "public")
    with pytest.raises(auth.AuthConfigError):
        auth.create_token(path, "alice", "public")
    with pytest.raises(auth.AuthConfigError):
        auth.create_token(path, "bob", "secret")
    assert auth.revoke_token(path, "alice") and auth.load_token_file(path) == []


# ------------------------------------------------------------------ Entra ID


class FakeJwks:
    def __init__(self, public_key):
        self.public_key = public_key

    def get_signing_key_from_jwt(self, token):
        return type("Key", (), {"key": self.public_key})()


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


ENTRA = auth.EntraSettings(tenant_id="tenant-1", audience="api://policy-evidence")


def entra_token(private_key, **overrides) -> str:
    claims = {
        "iss": ENTRA.issuer,
        "aud": ENTRA.audience,
        "exp": int(time.time()) + 600,
        "oid": "user-oid-1",
        "azp": "client-app",
        "scp": "Evidence.Read",
        "roles": ["Evidence.Internal"],
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


async def test_entra_token_maps_app_role_to_clearance(keypair):
    private, public = keypair
    verifier = auth.EntraTokenVerifier(ENTRA, jwks_client=FakeJwks(public))
    access = await verifier.verify_token(entra_token(private))
    assert access.subject == "user-oid-1" and access.claims["clearance"] == "internal"
    both = await verifier.verify_token(
        entra_token(private, roles=["Evidence.Public", "Evidence.Restricted"])
    )
    assert both.claims["clearance"] == "restricted"  # the highest role granted
    none = await verifier.verify_token(entra_token(private, roles=[]))
    assert none.claims["clearance"] == "public"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "api://another-api"},
        {"iss": "https://login.microsoftonline.com/other-tenant/v2.0"},
        {"exp": int(time.time()) - 60},
        {"scp": "User.Read"},
    ],
    ids=["wrong audience", "wrong issuer", "expired", "missing scope"],
)
async def test_entra_rejects_invalid_tokens(keypair, overrides):
    private, public = keypair
    verifier = auth.EntraTokenVerifier(ENTRA, jwks_client=FakeJwks(public))
    assert await verifier.verify_token(entra_token(private, **overrides)) is None


async def test_entra_rejects_a_token_signed_by_another_key(keypair):
    _, public = keypair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = auth.EntraTokenVerifier(ENTRA, jwks_client=FakeJwks(public))
    assert await verifier.verify_token(entra_token(other)) is None


# ------------------------------------------------------------------ effective ceiling


@pytest.mark.parametrize(
    "server,caller,expected",
    [
        ("internal", "restricted", "internal"),
        ("restricted", "internal", "internal"),
        ("internal", "public", "public"),
        ("internal", "nonsense", "public"),
    ],
)
def test_effective_ceiling_is_the_lower_of_server_and_caller(server, caller, expected):
    from mcp.server.auth.provider import AccessToken

    token = AccessToken(token="t", client_id="c", scopes=[], claims={"clearance": caller})
    reset = auth_context_var.set(AuthenticatedUser(token))
    try:
        assert auth.effective_ceiling(server) == expected
    finally:
        auth_context_var.reset(reset)


def test_no_caller_means_the_server_ceiling():
    assert auth.effective_ceiling("internal") == "internal"


def test_cli_refuses_unauthenticated_network_binding(capsys):
    code = main(["serve", "--transport", "streamable-http", "--host", "0.0.0.0"])
    assert code == 2
    assert "Refusing to serve without authentication" in capsys.readouterr().err


# ------------------------------------------------------------------ end to end over HTTP


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(sample_index, tmp_path):
    tokens = tmp_path / "tokens.json"
    public_token = auth.create_token(tokens, "alice", "public")
    internal_token = auth.create_token(tokens, "bob", "internal")
    port = _free_port()
    env = {
        "EVIDENCE_MCP_INDEX_PATH": str(sample_index),
        "EVIDENCE_MCP_CACHE_DIR": str(tmp_path / "cache"),
        "EVIDENCE_MCP_MAX_CLASSIFICATION": "internal",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "evidence_mcp",
            "serve",
            "--transport",
            "streamable-http",
            "--port",
            str(port),
            "--auth",
            "tokens",
            "--tokens-file",
            str(tokens),
        ],
        env={**__import__("os").environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield url, public_token, internal_token
    proc.terminate()
    proc.wait(timeout=10)


async def _documents(url: str, token: str) -> dict:
    client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    async with Client(streamable_http_client(url, http_client=client)) as session:
        result = await session.call_tool("list_documents", {})
    return json.loads(result.content[0].text)


async def test_http_requires_a_token_and_filters_by_clearance(http_server):
    url, public_token, internal_token = http_server
    async with httpx2.AsyncClient() as plain:
        response = await plain.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert response.status_code == 401
    assert "resource_metadata" in response.headers.get("www-authenticate", "")

    public_view = await _documents(url, public_token)
    internal_view = await _documents(url, internal_token)
    assert public_view["clearance"] == "public" and internal_view["clearance"] == "internal"
    public_levels = {d["classification"] for d in public_view["documents"]}
    internal_levels = {d["classification"] for d in internal_view["documents"]}
    assert public_levels == {"public"}
    assert "internal" in internal_levels and "restricted" not in internal_levels


def test_hosts_are_accepted_with_and_without_a_port():
    """Behind a TLS proxy (Copilot Studio, an ingress) the Host header has no port."""
    from mcp.server.transport_security import TransportSecurityMiddleware

    from evidence_mcp.cli import transport_security

    check = TransportSecurityMiddleware(transport_security(["evidence.example.org"]))
    assert check._validate_host("evidence.example.org")
    assert check._validate_host("evidence.example.org:443")
    assert check._validate_host("127.0.0.1:8000")
    assert not check._validate_host("attacker.example")
    assert check._validate_origin("https://evidence.example.org")
    assert not check._validate_origin("https://attacker.example")
