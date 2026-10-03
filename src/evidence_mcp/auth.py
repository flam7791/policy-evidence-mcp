"""Access management for the HTTP transport: who is calling, and what they may see.

Two ways to authenticate a bearer token, both returning the caller's **clearance**:

- `StaticTokenVerifier`: tokens issued by `evidence-mcp token create`, stored only as SHA-256
  hashes with a name and a clearance. For a team server or a pilot.
- `EntraTokenVerifier`: OAuth 2.0 access tokens from Microsoft Entra ID (or any OIDC issuer),
  validated against the issuer's published keys: signature, issuer, audience, expiry and the
  required scope. The clearance comes from the token's app roles, so it is managed in Entra
  (assigning users or groups to roles), not in this server.

Each request then sees documents at or below the lower of the server's ceiling and the caller's
clearance: a restricted-cleared user on an internal server sees internal documents; an
internal-cleared user on a restricted server does not see restricted ones. Statistics tools are
public data and need only a valid token.

stdio (one local user, one process) needs no token: the server's ceiling applies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

from .config import CLASSIFICATION_LEVELS

READ_SCOPE = "evidence.read"


class AuthConfigError(ValueError):
    pass


def lower_of(a: str, b: str) -> str:
    """The less permissive of two classification levels."""
    rank = {name: i for i, name in enumerate(CLASSIFICATION_LEVELS)}
    return a if rank.get(a, -1) <= rank.get(b, -1) else b


def effective_ceiling(server_ceiling: str) -> str:
    """The ceiling for the current request: the server's, lowered to the caller's clearance.

    With no authenticated caller (stdio) the server's ceiling applies. An authenticated token
    without a recognised clearance gets "public", never more.
    """
    token = get_access_token()
    if token is None:
        return server_ceiling
    clearance = (token.claims or {}).get("clearance", "public")
    if clearance not in CLASSIFICATION_LEVELS:
        clearance = "public"
    return lower_of(server_ceiling, clearance)


def caller() -> str | None:
    token = get_access_token()
    return token.subject or token.client_id if token else None


# ----------------------------------------------------------------- static tokens


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TokenRecord:
    name: str
    clearance: str
    sha256: str


def load_token_file(path: Path) -> list[TokenRecord]:
    if not path.exists():
        raise AuthConfigError(f"token file not found: {path} (create one with `token create`)")
    records = []
    for item in json.loads(path.read_text(encoding="utf-8")):
        if item.get("clearance") not in CLASSIFICATION_LEVELS:
            raise AuthConfigError(f"token {item.get('name')!r}: unknown clearance")
        records.append(TokenRecord(item["name"], item["clearance"], item["sha256"]))
    return records


def create_token(path: Path, name: str, clearance: str) -> str:
    """Append a new token to the file and return it. Only its hash is stored."""
    if clearance not in CLASSIFICATION_LEVELS:
        raise AuthConfigError(f"clearance must be one of {', '.join(CLASSIFICATION_LEVELS)}")
    records = []
    if path.exists():
        records = json.loads(path.read_text(encoding="utf-8"))
    if any(r["name"] == name for r in records):
        raise AuthConfigError(f"a token named {name!r} exists; revoke it first")
    token = "emcp_" + secrets.token_urlsafe(32)
    records.append(
        {
            "name": name,
            "clearance": clearance,
            "sha256": _sha256(token),
            "created": time.strftime("%Y-%m-%d"),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return token


def revoke_token(path: Path, name: str) -> bool:
    records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    kept = [r for r in records if r["name"] != name]
    path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    return len(kept) != len(records)


class StaticTokenVerifier:
    def __init__(self, records: list[TokenRecord]):
        self.records = records

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = _sha256(token)
        for record in self.records:
            if hmac.compare_digest(digest, record.sha256):  # constant time
                return AccessToken(
                    token="<redacted>",
                    client_id=record.name,
                    subject=record.name,
                    scopes=[READ_SCOPE],
                    claims={"clearance": record.clearance, "iss": "evidence-mcp"},
                )
        return None


# ----------------------------------------------------------------- Entra ID / OIDC


@dataclass(frozen=True)
class EntraSettings:
    """Settings for validating Entra ID access tokens (see docs/entra-id.md).

    tenant_id    Directory (tenant) ID.
    audience     The API's Application ID URI or client ID, e.g. api://policy-evidence.
    required_scope  Delegated scope the client must hold, e.g. Evidence.Read.
    role_clearance  App role value -> clearance, e.g. {"Evidence.Internal": "internal"}.
    """

    tenant_id: str
    audience: str
    required_scope: str = "Evidence.Read"
    role_clearance: tuple[tuple[str, str], ...] = (
        ("Evidence.Public", "public"),
        ("Evidence.Internal", "internal"),
        ("Evidence.Restricted", "restricted"),
    )

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    @property
    def jwks_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"

    @classmethod
    def from_env(cls) -> EntraSettings:
        env = os.environ
        tenant, audience = (
            env.get("EVIDENCE_MCP_ENTRA_TENANT_ID"),
            env.get("EVIDENCE_MCP_ENTRA_AUDIENCE"),
        )
        if not tenant or not audience:
            raise AuthConfigError(
                "Entra mode needs EVIDENCE_MCP_ENTRA_TENANT_ID and EVIDENCE_MCP_ENTRA_AUDIENCE"
            )
        kwargs = {}
        if env.get("EVIDENCE_MCP_ENTRA_SCOPE"):
            kwargs["required_scope"] = env["EVIDENCE_MCP_ENTRA_SCOPE"]
        if env.get("EVIDENCE_MCP_ENTRA_ROLES"):  # "Role.A=public,Role.B=internal"
            pairs = [p.split("=", 1) for p in env["EVIDENCE_MCP_ENTRA_ROLES"].split(",") if p]
            kwargs["role_clearance"] = tuple((k.strip(), v.strip()) for k, v in pairs)
        return cls(tenant, audience, **kwargs)


class EntraTokenVerifier:
    """Validates RS256 access tokens against the issuer's JSON Web Key Set."""

    def __init__(self, settings: EntraSettings, jwks_client=None):
        import jwt  # PyJWT, a dependency of the MCP SDK

        self.jwt = jwt
        self.settings = settings
        self.jwks = jwks_client or jwt.PyJWKClient(settings.jwks_url, cache_keys=True)
        for _, clearance in settings.role_clearance:
            if clearance not in CLASSIFICATION_LEVELS:
                raise AuthConfigError(f"unknown clearance {clearance!r} in role mapping")

    def clearance_for(self, roles: list[str]) -> str:
        mapping = dict(self.settings.role_clearance)
        granted = [mapping[r] for r in roles if r in mapping]
        if not granted:
            return "public"
        rank = {name: i for i, name in enumerate(CLASSIFICATION_LEVELS)}
        return max(granted, key=rank.__getitem__)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = self.jwks.get_signing_key_from_jwt(token).key
            claims = self.jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except Exception:  # noqa: BLE001 - any validation failure means "not authenticated"
            return None
        scopes = str(claims.get("scp", "")).split()
        if self.settings.required_scope not in scopes:
            return None
        roles = list(claims.get("roles") or [])
        subject = claims.get("oid") or claims.get("sub")
        return AccessToken(
            token="<redacted>",
            client_id=str(claims.get("azp") or claims.get("appid") or "unknown-client"),
            subject=str(subject) if subject else None,
            scopes=[READ_SCOPE, *scopes],
            expires_at=int(claims["exp"]),
            claims={"clearance": self.clearance_for(roles), "iss": claims["iss"], "roles": roles},
        )
