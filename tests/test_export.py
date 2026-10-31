"""Turning a run into deliverables: subject lines, the cleaned email, the one-file HTML and the zip."""

import base64
import io
import zipfile

import export
import pytest
from conftest import FILES_URL

RUN = "run123"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

EMAIL = f"""<html><body style="margin:0"><div style="display:none;max-height:0">Preview text</div>
<table>
<!-- Subject/Preheader Section -->
<tr><td>Subject options for the reviewer</td></tr>
    <!-- Section:Start hero -->
<tr><td><img src="{FILES_URL}/runs/{RUN}/hero.png"><img src="{FILES_URL}/brands/acme/hero.png">
<img src="https://cdn.example.org/remote.png"><img src="{FILES_URL}/../../outside.png">
<a href="https://example.com/your-link">Shop</a></td></tr>
    <!-- Section:End hero -->
</table></body></html>"""

MJML = """<mjml><mj-head><mj-preview>Preview text</mj-preview></mj-head><mj-body>
<!-- Subject/Preheader Section --><mj-section><mj-column><mj-text>options</mj-text></mj-column></mj-section>
<mj-section><mj-column><mj-text>hero</mj-text></mj-column></mj-section></mj-body></mjml>"""

TOPPERS = """
<div class="email-topper"><div><strong>Subject:</strong> Meet the Tree Runner</div>
  <div><strong>Preheader:</strong> Light &amp; breezy <b>wool</b></div></div>
<div class="email-topper"><div><strong>Subject:</strong> Second option</div><div><strong>Preheader:</strong> Two</div></div>
<div class="email-topper"><div>no labels here</div></div>
"""


@pytest.fixture
def run(storage):
    d = storage / "runs" / RUN
    d.mkdir(parents=True)
    (d / "email.html").write_text(EMAIL)
    (d / "email.mjml").write_text(MJML)
    (d / "toppers.html").write_text(TOPPERS)
    (d / "hero.png").write_bytes(PNG)
    (storage / "brands" / "acme").mkdir(parents=True)
    (storage / "brands" / "acme" / "hero.png").write_bytes(PNG + b"other")
    return d


def test_parse_subject_lines_reads_each_option():
    assert export.parse_subject_lines(TOPPERS) == [
        {"subject": "Meet the Tree Runner", "preheader": "Light & breezy wool"},
        {"subject": "Second option", "preheader": "Two"},
    ]
    assert export.parse_subject_lines("") == []


def test_subject_lines_text():
    assert export.subject_lines_text([]) == "No subject lines were generated for this run.\n"
    text = export.subject_lines_text([{"subject": "S", "preheader": "P"}])
    assert "Option 1\nSubject:   S\nPreheader: P" in text


@pytest.mark.parametrize("bad", ["", "../etc", "a/b", "x" * 65])
def test_run_dir_rejects_unsafe_ids(bad):
    with pytest.raises(ValueError):
        export.run_dir(bad)


def test_run_dir_needs_a_finished_email(storage):
    with pytest.raises(FileNotFoundError):
        export.run_dir("nothere")


def test_ensure_exports_splits_out_the_subject_lines(run):
    result = export.ensure_exports(RUN)
    assert [o["subject"] for o in result["subject_lines"]] == ["Meet the Tree Runner", "Second option"]
    assert result["placeholder_links"] == ["https://example.com/your-link"]

    html = (run / "email.html").read_text()
    assert "Subject/Preheader Section" not in html and "Subject options for the reviewer" not in html
    assert "    <!-- Section:Start hero -->" in html  # the next section keeps its indentation
    assert '<div style="display:none;max-height:0">Light &amp; breezy wool</div>' in html

    mjml = (run / "email.mjml").read_text()
    assert "options" not in mjml and "<mj-preview>Light &amp; breezy wool</mj-preview>" in mjml
    assert "Option 2\nSubject:   Second option" in (run / "subject-lines.txt").read_text()
    assert (run / "original" / "email.html").read_text() == EMAIL


def test_ensure_exports_is_idempotent(run):
    export.ensure_exports(RUN)
    first = (run / "email.html").read_text()
    export.ensure_exports(RUN)
    assert (run / "email.html").read_text() == first
    assert (run / "original" / "email.html").read_text() == EMAIL


def test_standalone_html_inlines_local_images_only(run):
    export.ensure_exports(RUN)
    doc = export.standalone_html(RUN).decode()
    assert f'src="data:image/png;base64,{base64.b64encode(PNG).decode()}"' in doc
    assert "https://cdn.example.org/remote.png" in doc
    assert f"{FILES_URL}/../../outside.png" in doc  # outside storage: never read from disk


def test_bundle_zip_relinks_images_and_keeps_same_named_files_apart(run):
    export.ensure_exports(RUN)
    z = zipfile.ZipFile(io.BytesIO(export.bundle_zip(RUN)))
    assert sorted(z.namelist()) == ["email.html", "email.mjml", "images/hero-2.png", "images/hero.png", "subject-lines.txt"]
    html = z.read("email.html").decode()
    assert 'src="images/hero.png"' in html and 'src="images/hero-2.png"' in html
    assert z.read("images/hero.png") == PNG and z.read("images/hero-2.png") == PNG + b"other"
