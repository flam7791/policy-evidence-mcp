"""Sync a SharePoint document library into a local corpus through Microsoft Graph.

The classification of every file comes from its **Microsoft Purview sensitivity label**, mapped
in the configuration, and a file whose label is missing or unknown is treated as the most
sensitive level, so a labelling gap can never widen access. The result is an ordinary corpus
folder (`<out>/<classification>/...` plus a `.meta.json` sidecar with the document's web URL),
which `evidence-mcp ingest` indexes as usual: answers cite the SharePoint link.

Authentication is app-only (client credentials) with the least privilege Graph offers for this:
**Sites.Selected**, granted read access to the one site. The client secret, or a certificate in a
production setup, comes from the environment or a secret store, never from the config file.

Incremental: files whose eTag has not changed since the last sync are not downloaded again.

Configuration (TOML), for example `sharepoint.toml`:

    tenant_id = "00000000-0000-0000-0000-000000000000"
    client_id = "11111111-1111-1111-1111-111111111111"
    site = "contoso.sharepoint.com:/sites/policy"
    library = "Documents"
    folder = "Published"
    unlabelled = "restricted"

    [labels]   # Purview sensitivity label id -> classification
    "aaaaaaaa-0000-0000-0000-000000000001" = "public"
    "aaaaaaaa-0000-0000-0000-000000000002" = "internal"
    "aaaaaaaa-0000-0000-0000-000000000003" = "restricted"
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx

from .config import CLASSIFICATION_LEVELS
from .documents import SUPPORTED_SUFFIXES

GRAPH = "https://graph.microsoft.com/v1.0"
MANIFEST = ".sharepoint-sync.json"


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class SharePointConfig:
    tenant_id: str
    client_id: str
    site: str  # "<hostname>:/sites/<name>"
    library: str = "Documents"
    folder: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    unlabelled: str = "restricted"
    max_file_mb: int = 50

    def __post_init__(self):
        for value in [*self.labels.values(), self.unlabelled]:
            if value not in CLASSIFICATION_LEVELS:
                raise SyncError(f"unknown classification {value!r} in the label mapping")
        if ":/" not in self.site:
            raise SyncError("site must look like 'contoso.sharepoint.com:/sites/policy'")

    @classmethod
    def load(cls, path: Path) -> SharePointConfig:
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10
            import tomli as tomllib  # type: ignore[no-redef]
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        data["labels"] = {k.lower(): v for k, v in (data.get("labels") or {}).items()}
        return cls(**data)


class GraphClient:
    """Minimal Microsoft Graph client: client-credentials token, paging, 429 back-off."""

    def __init__(
        self,
        config: SharePointConfig,
        secret: str | None = None,
        http: httpx.Client | None = None,
        sleep=time.sleep,
    ):
        self.config = config
        self.secret = (
            secret if secret is not None else os.environ.get("EVIDENCE_MCP_GRAPH_CLIENT_SECRET", "")
        )
        if not self.secret:
            raise SyncError("set EVIDENCE_MCP_GRAPH_CLIENT_SECRET (from a secret store)")
        self.http = http or httpx.Client(timeout=60, follow_redirects=True)
        self.sleep = sleep
        self._token: tuple[str, float] | None = None

    def _bearer(self) -> str:
        if self._token and self._token[1] > time.time() + 60:
            return self._token[0]
        response = self.http.post(
            f"https://login.microsoftonline.com/{self.config.tenant_id}/oauth2/v2.0/token",
            data={
                "client_id": self.config.client_id,
                "client_secret": self.secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
        )
        if response.status_code != 200:
            raise SyncError(f"token request failed ({response.status_code}): check the app")
        body = response.json()
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return self._token[0]

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        url = url if url.startswith("https://") else GRAPH + url
        for attempt in range(5):
            response = self.http.request(
                method, url, headers={"Authorization": f"Bearer {self._bearer()}"}, **kwargs
            )
            if response.status_code in (429, 503) and attempt < 4:
                self.sleep(min(int(response.headers.get("Retry-After", 2**attempt)), 60))
                continue
            if response.status_code >= 400:
                raise SyncError(f"Graph {method} {url} -> {response.status_code}")
            return response
        raise SyncError(f"Graph {method} {url}: still throttled")

    def paged(self, url: str):
        while url:
            body = self.request("GET", url).json()
            yield from body.get("value", [])
            url = body.get("@odata.nextLink")


@dataclass
class SyncReport:
    downloaded: int = 0
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)
    unlabelled: list[str] = field(default_factory=list)
    by_classification: dict[str, int] = field(default_factory=dict)


def _classify(config: SharePointConfig, labels: list[dict]) -> tuple[str, str | None]:
    """(classification, label id). Unlabelled or unknown labels get config.unlabelled."""
    rank = {name: i for i, name in enumerate(CLASSIFICATION_LEVELS)}
    found = [
        (config.labels.get(str(lab.get("sensitivityLabelId", "")).lower()), lab) for lab in labels
    ]
    known = [(c, lab) for c, lab in found if c]
    if not known:
        return config.unlabelled, None
    level, lab = max(known, key=lambda x: rank[x[0]])  # most sensitive wins
    return level, lab.get("sensitivityLabelId")


def _walk(client: GraphClient, drive_id: str, folder: str):
    path = folder.strip("/")
    url = (
        f"/drives/{drive_id}/root:/{quote(path)}:/children"
        if path
        else f"/drives/{drive_id}/root/children"
    )
    for item in client.paged(url):
        if "folder" in item:
            yield from _walk(client, drive_id, f"{path}/{item['name']}".strip("/"))
        elif "file" in item:
            yield path, item


def sync(config: SharePointConfig, out_dir: Path, client: GraphClient) -> SyncReport:
    site = client.request("GET", f"/sites/{config.site}").json()
    drives = list(client.paged(f"/sites/{site['id']}/drives"))
    drive = next((d for d in drives if d.get("name") == config.library), None)
    if drive is None:
        raise SyncError(f"library {config.library!r} not found on {config.site}")

    manifest_path = out_dir / MANIFEST
    previous = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest, report = {}, SyncReport()

    for folder, item in _walk(client, drive["id"], config.folder):
        name = item["name"]
        relative = PurePosixPath(folder.removeprefix(config.folder.strip("/")).strip("/")) / name
        if PurePosixPath(name).suffix.lower() not in SUPPORTED_SUFFIXES:
            report.skipped.append(f"{relative} (format)")
            continue
        if item.get("size", 0) > config.max_file_mb * 1024 * 1024:
            report.skipped.append(f"{relative} (size)")
            continue
        labels = (
            client.request(
                "POST", f"/drives/{drive['id']}/items/{item['id']}/extractSensitivityLabels"
            )
            .json()
            .get("labels", [])
        )
        level, label_id = _classify(config, labels)
        if label_id is None:
            report.unlabelled.append(str(relative))
        target = out_dir / level / relative
        old = previous.get(item["id"])
        if (
            old
            and old["etag"] == item.get("eTag")
            and old["path"] == target.as_posix()
            and target.exists()
        ):
            report.unchanged += 1
        else:
            if old and Path(old["path"]).exists() and old["path"] != target.as_posix():
                Path(old["path"]).unlink()  # re-labelled: never leave a copy at the old level
            content = client.request(
                "GET", f"/drives/{drive['id']}/items/{item['id']}/content"
            ).content
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            sidecar = {
                "title": PurePosixPath(name).stem,
                "source": item.get("webUrl", ""),
                "classification": level,
                "sensitivity_label_id": label_id,
                "modified": item.get("lastModifiedDateTime"),
            }
            target.with_name(target.name + ".meta.json").write_text(
                json.dumps(sidecar, indent=2) + "\n", encoding="utf-8"
            )
            report.downloaded += 1
        manifest[item["id"]] = {"etag": item.get("eTag"), "path": target.as_posix()}
        report.by_classification[level] = report.by_classification.get(level, 0) + 1

    # Files deleted in SharePoint are removed from the local corpus too.
    for item_id, old in previous.items():
        if item_id not in manifest and Path(old["path"]).exists():
            Path(old["path"]).unlink()
            side = Path(old["path"] + ".meta.json")
            if side.exists():
                side.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return report
