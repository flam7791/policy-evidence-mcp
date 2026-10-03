"""SharePoint sync against a simulated Microsoft Graph: labels decide the classification,
unlabelled files fail closed, syncs are incremental, re-labelled and deleted files move or go."""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import pytest

from evidence_mcp.documents import iter_corpus
from evidence_mcp.sharepoint import GraphClient, SharePointConfig, SyncError, sync

PUBLIC, INTERNAL, RESTRICTED = (f"aaaaaaaa-0000-0000-0000-00000000000{i}" for i in (1, 2, 3))


def docx(title: str, *paragraphs: str) -> bytes:
    """A minimal Word document: a Title paragraph and body paragraphs."""
    body = f'<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>{title}</w:t></w:r></w:p>'
    body += "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("word/document.xml", xml)
    return buffer.getvalue()


class FakeGraph:
    """Token endpoint, site, drives, folder listing with paging, labels and file content."""

    def __init__(self):
        self.files = {
            "f1": {
                "name": "Travel policy.md",
                "folder": "Published",
                "label": PUBLIC,
                "content": b"# Travel policy\n\nEconomy class under six hours.\n",
            },
            "f2": {
                "name": "Budget note.docx",
                "folder": "Published/Finance",
                "label": INTERNAL,
                "content": docx("Budget note", "The travel budget is 1.2 million euros."),
            },
            "f3": {
                "name": "Unlabelled draft.md",
                "folder": "Published",
                "label": None,
                "content": b"# Draft\n\nNot yet labelled.\n",
            },
            "f4": {"name": "Photo.png", "folder": "Published", "label": None, "content": b"png"},
        }
        self.etags = {k: "v1" for k in self.files}
        self.calls: list[str] = []
        self.token_requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url, path = str(request.url), request.url.path
        self.calls.append(f"{request.method} {path}")
        if "login.microsoftonline.com" in url:
            self.token_requests += 1
            assert b"client_credentials" in request.content
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer t"
        if path == "/v1.0/sites/contoso.sharepoint.com:/sites/policy":
            return httpx.Response(200, json={"id": "site-1"})
        if path == "/v1.0/sites/site-1/drives":
            return httpx.Response(200, json={"value": [{"id": "drive-1", "name": "Documents"}]})
        if path.endswith(":/children"):
            folder = request.url.path.split("root:/", 1)[1].rsplit(":/children", 1)[0]
            items = [
                {
                    "id": k,
                    "name": v["name"],
                    "file": {},
                    "eTag": self.etags[k],
                    "webUrl": f"https://contoso.sharepoint.com/{v['folder']}/{v['name']}",
                    "size": len(v["content"]),
                }
                for k, v in self.files.items()
                if v["folder"] == folder
            ]
            if folder == "Published":
                items.append({"id": "d1", "name": "Finance", "folder": {"childCount": 1}})
                # second page, to exercise @odata.nextLink
                if "page=2" not in url:
                    return httpx.Response(
                        200,
                        json={
                            "value": items[:1],
                            "@odata.nextLink": "https://graph.microsoft.com/v1.0/drives/drive-1/"
                            "root:/Published:/children?page=2",
                        },
                    )
                return httpx.Response(200, json={"value": items[1:]})
            return httpx.Response(200, json={"value": items})
        if path.endswith("/extractSensitivityLabels"):
            item = path.split("/items/")[1].split("/")[0]
            label = self.files[item]["label"]
            labels = (
                [{"sensitivityLabelId": label, "assignmentMethod": "standard"}] if label else []
            )
            return httpx.Response(200, json={"labels": labels})
        if path.endswith("/content"):
            item = path.split("/items/")[1].split("/")[0]
            return httpx.Response(200, content=self.files[item]["content"])
        return httpx.Response(404)


@pytest.fixture
def graph():
    return FakeGraph()


@pytest.fixture
def config():
    return SharePointConfig(
        tenant_id="tenant",
        client_id="client",
        site="contoso.sharepoint.com:/sites/policy",
        folder="Published",
        labels={PUBLIC: "public", INTERNAL: "internal", RESTRICTED: "restricted"},
    )


def client(config, graph) -> GraphClient:
    return GraphClient(
        config,
        secret="s",
        http=httpx.Client(transport=httpx.MockTransport(graph.handler)),
        sleep=lambda s: None,
    )


def test_labels_decide_the_folder_and_unlabelled_fails_closed(config, graph, tmp_path):
    report = sync(config, tmp_path, client(config, graph))
    assert (tmp_path / "public" / "Travel policy.md").exists()
    assert (tmp_path / "internal" / "Finance" / "Budget note.docx").exists()
    assert (tmp_path / "restricted" / "Unlabelled draft.md").exists()  # never public
    assert report.unlabelled == ["Unlabelled draft.md"]
    assert report.skipped == ["Photo.png (format)"]
    assert report.by_classification == {"public": 1, "internal": 1, "restricted": 1}
    side = json.loads((tmp_path / "public" / "Travel policy.md.meta.json").read_text())
    assert side["source"].startswith("https://contoso.sharepoint.com/")
    assert graph.token_requests == 1  # the token is reused


def test_synced_corpus_indexes_with_sharepoint_citations(config, graph, tmp_path):
    sync(config, tmp_path, client(config, graph))
    docs = {d.title: d for d in iter_corpus(tmp_path)}
    budget = docs["Budget note"]
    assert budget.classification == "internal"
    assert budget.source.endswith("Budget note.docx")
    assert any("1.2 million" in text for _, _, text in budget.blocks)


def test_second_sync_downloads_only_changes(config, graph, tmp_path):
    sync(config, tmp_path, client(config, graph))
    graph.calls.clear()
    graph.etags["f1"] = "v2"
    report = sync(config, tmp_path, client(config, graph))
    assert report.downloaded == 1 and report.unchanged == 2
    assert sum(c.endswith("/content") for c in graph.calls) == 1


def test_relabelled_file_moves_and_deleted_file_goes(config, graph, tmp_path):
    sync(config, tmp_path, client(config, graph))
    graph.files["f1"]["label"] = RESTRICTED
    graph.etags["f1"] = "v2"
    del graph.files["f3"]
    sync(config, tmp_path, client(config, graph))
    assert not (tmp_path / "public" / "Travel policy.md").exists()
    assert (tmp_path / "restricted" / "Travel policy.md").exists()
    assert not (tmp_path / "restricted" / "Unlabelled draft.md").exists()


def test_throttling_is_retried(config, graph, tmp_path):
    original, state = graph.handler, {"n": 0}

    def flaky(request):
        if request.url.path.endswith("/drives") and state["n"] == 0:
            state["n"] += 1
            return httpx.Response(429, headers={"Retry-After": "1"})
        return original(request)

    c = GraphClient(
        config,
        secret="s",
        http=httpx.Client(transport=httpx.MockTransport(flaky)),
        sleep=lambda s: None,
    )
    assert sync(config, tmp_path, c).downloaded == 3


def test_config_validation(tmp_path):
    with pytest.raises(SyncError):
        SharePointConfig(tenant_id="t", client_id="c", site="no-colon", labels={})
    with pytest.raises(SyncError):
        SharePointConfig(tenant_id="t", client_id="c", site="h:/sites/x", labels={"x": "secret"})
    path = tmp_path / "sp.toml"
    path.write_text('tenant_id="t"\nclient_id="c"\nsite="h:/sites/x"\n[labels]\n"AB"="internal"\n')
    assert SharePointConfig.load(path).labels == {"ab": "internal"}


def test_secret_is_required(config, monkeypatch):
    monkeypatch.delenv("EVIDENCE_MCP_GRAPH_CLIENT_SECRET", raising=False)
    with pytest.raises(SyncError, match="EVIDENCE_MCP_GRAPH_CLIENT_SECRET"):
        GraphClient(config)
