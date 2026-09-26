from pathlib import Path

from evidence_mcp.documents import chunk_document, clean_text, iter_corpus, load_document

from .conftest import SAMPLE_CORPUS


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_front_matter_sets_metadata(tmp_path):
    write(
        tmp_path / "a.md",
        "---\ntitle: Travel Rules\nclassification: internal\n---\n# Ignored\nText.",
    )
    doc = load_document(tmp_path / "a.md", tmp_path)
    assert doc.title == "Travel Rules"
    assert doc.classification == "internal"


def test_folder_name_sets_classification_when_no_front_matter(tmp_path):
    write(tmp_path / "restricted" / "b.md", "# Budget\n\nNumbers.")
    doc = load_document(tmp_path / "restricted" / "b.md", tmp_path)
    assert doc.classification == "restricted"
    assert doc.title == "Budget"


def test_unlabelled_documents_default_to_public(tmp_path):
    write(tmp_path / "notes" / "c.txt", "Plain text.")
    assert load_document(tmp_path / "notes" / "c.txt", tmp_path).classification == "public"


def test_chunks_never_cross_a_section_heading(tmp_path):
    write(tmp_path / "d.md", "# Doc\n\n## One\n\nAlpha.\n\n## Two\n\nBeta.")
    chunks = chunk_document(load_document(tmp_path / "d.md", tmp_path))
    assert [(c.section, c.text) for c in chunks] == [("One", "Alpha."), ("Two", "Beta.")]
    assert chunks[0].chunk_id == "d#1"


def test_chunks_respect_max_chars(tmp_path):
    sentence = "This is a sentence about retention. "
    write(tmp_path / "e.md", "# Doc\n\n## Long\n\n" + sentence * 100)
    chunks = chunk_document(load_document(tmp_path / "e.md", tmp_path), max_chars=300)
    assert len(chunks) > 5
    assert all(len(c.text) <= 300 for c in chunks)


def test_control_characters_are_removed():
    assert clean_text("safe​text\x07") == "safetext"


def test_readme_files_are_not_indexed():
    paths = [doc.path for doc in iter_corpus(SAMPLE_CORPUS)]
    assert "README.md" not in paths
    assert "public/ai-use-policy.md" in paths


def test_citation_format(tmp_path):
    write(tmp_path / "f.md", "# Policy\n\n## Scope\n\nApplies to all.")
    chunk = chunk_document(load_document(tmp_path / "f.md", tmp_path))[0]
    assert chunk.citation() == "Policy, Scope [f#1]"


def test_pdf_pages_become_page_level_citations(tmp_path):
    import shutil

    from .conftest import FIXTURES

    shutil.copy(FIXTURES / "sample_report.pdf", tmp_path / "report.pdf")
    doc = load_document(tmp_path / "report.pdf", tmp_path)
    chunks = chunk_document(doc)
    assert doc.title == "Sample Report on Remote Work"
    assert [c.page for c in chunks] == [1, 2]
    assert "two office days" in chunks[1].text
    assert chunks[1].citation() == "Sample Report on Remote Work, page 2 [report#2]"
