# SharePoint and Microsoft Graph: syncing a library into the evidence server

`evidence-mcp sync-sharepoint` copies one document library (or one folder of it) into a local
corpus through Microsoft Graph, classifying each file by its **Purview sensitivity label**. The
evidence server then indexes it as usual, and its answers cite the SharePoint link.

```bash
export EVIDENCE_MCP_GRAPH_CLIENT_SECRET=...      # from a secret store
evidence-mcp sync-sharepoint --config sharepoint.toml --out corpus-sharepoint
evidence-mcp ingest --corpus corpus-sharepoint --ceiling internal
```

Run it on a schedule (a container job, a pipeline, a scheduled task); it downloads only files
whose eTag changed, moves a file when its label changes, and removes files deleted in
SharePoint.

## 1. App registration (app-only, least privilege)

Entra admin centre > App registrations > New registration: `evidence-sharepoint-sync`, single
tenant, no redirect URI.

**API permissions** > Microsoft Graph > Application permissions > **`Sites.Selected`**, then grant
admin consent. On its own, `Sites.Selected` gives access to no site at all; the next step grants
read access to the one site the sync needs, instead of every site in the tenant
(`Sites.Read.All`).

**Certificates & secrets**: a client secret for a pilot (stored in a secret store, rotated), a
certificate for production.

Check the permissions that Microsoft Graph documents for `driveItem: extractSensitivityLabels` in
your tenant before going live: label extraction is what the classification depends on.

## 2. Grant the app read access to one site

A SharePoint or Global administrator grants the site permission, for example with Graph:

```http
POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
Content-Type: application/json

{
  "roles": ["read"],
  "grantedToIdentities": [
    { "application": { "id": "<client id of evidence-sharepoint-sync>",
                       "displayName": "evidence-sharepoint-sync" } }
  ]
}
```

## 3. Map sensitivity labels to classifications

Label ids are listed in the Microsoft Purview portal (Information protection > Labels). Map each
label the library uses; anything not mapped, and any file without a label, is treated as
`unlabelled` (default `restricted`), so a labelling gap can only narrow access:

```toml
unlabelled = "restricted"
[labels]
"<id of Public>"       = "public"
"<id of General>"      = "internal"
"<id of Confidential>" = "restricted"
```

A file with several labels takes the most sensitive one.

## 4. What the sync writes

```
corpus-sharepoint/
  public/Travel policy.md
  public/Travel policy.md.meta.json        title, source (SharePoint web URL), label id, modified
  internal/Finance/Budget note.docx
  internal/Finance/Budget note.docx.meta.json
  .sharepoint-sync.json                    item id -> eTag and local path (for incremental sync)
```

Supported formats: Markdown, text, PDF and Word (`.docx`); others are reported as skipped.

## Limits

- One site and one library per configuration; run one sync per library.
- Item-level SharePoint permissions are not copied: the classification decides who sees a
  passage through the evidence server. Libraries with unique permissions per file should be
  split by classification, or not synced.
- The sync is pull-based. For near-real-time updates, Graph change notifications or delta queries
  are the next step.
