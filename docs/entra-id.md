# Microsoft Entra ID: app registration for the evidence server

What to configure so that people and assistants reach `policy-evidence-mcp` with their own
organisational identity, and see documents according to roles managed in Entra.

## 1. Register the API

Entra admin centre > App registrations > New registration.

| Field | Value |
|---|---|
| Name | `policy-evidence-mcp` |
| Supported account types | This organisational directory only |
| Redirect URI | none (this is an API) |

Then **Expose an API**:

- Application ID URI: `api://policy-evidence` (this is `EVIDENCE_MCP_ENTRA_AUDIENCE`)
- Add a scope: `Evidence.Read`, who can consent: admins only, description "Read statistics and
  documents through the evidence server"

**Manifest**: set `"requestedAccessTokenVersion": 2`, so tokens carry the v2.0 issuer the server
checks (`https://login.microsoftonline.com/<tenant>/v2.0`).

## 2. Define app roles for clearance

App roles > Create app role, allowed member types **Users/Groups**:

| Display name | Value | Grants |
|---|---|---|
| Evidence: public | `Evidence.Public` | public documents |
| Evidence: internal | `Evidence.Internal` | public and internal |
| Evidence: restricted | `Evidence.Restricted` | public, internal and restricted |

A token's clearance is its highest role; a token with none of them gets `public`. The mapping can
be changed with `EVIDENCE_MCP_ENTRA_ROLES` (for example to reuse existing role names).

## 3. Assign people, preferably by group

Enterprise applications > `policy-evidence-mcp` > Properties: **Assignment required = Yes**, so
nobody without a role gets a token at all. Users and groups > Add assignment: assign groups
(for example the information owners' security groups) to roles, rather than individuals.
Access reviews on those groups then govern who sees restricted documents.

## 4. Authorise the client application

The assistant or agent that calls the server (an MCP client, a Copilot Studio agent, the agents
service) is its own app registration. Under its **API permissions**, add `policy-evidence-mcp` >
`Evidence.Read` (delegated) and grant admin consent. It then obtains a token for
`api://policy-evidence/Evidence.Read` on behalf of the signed-in user, so the server sees the
user's roles, not the application's.

For unattended jobs (no user), define an application permission instead and assign the job's
service principal to the lowest role it needs.

## 5. Run the server

```bash
export EVIDENCE_MCP_ENTRA_TENANT_ID=<directory (tenant) id>
export EVIDENCE_MCP_ENTRA_AUDIENCE=api://policy-evidence
export EVIDENCE_MCP_MAX_CLASSIFICATION=restricted   # the server's own ceiling
evidence-mcp serve --transport streamable-http --host 0.0.0.0 --auth entra \
    --public-url https://evidence.example.org/mcp --allowed-host evidence.example.org
```

Put it behind the organisation's reverse proxy or ingress with TLS. The server checks on every
request: signature (keys fetched from the tenant's JWKS endpoint and cached), issuer, audience,
expiry and scope.

## What the server never does

- It never sees passwords or refresh tokens: only short-lived access tokens.
- It does not call Microsoft Graph for authorisation: everything it needs is in the token.
- It does not log tokens or query text; it logs the caller's object ID and the ceiling applied.
