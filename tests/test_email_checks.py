"""The checks every finished email goes through. The model, the link fetcher and MJML are faked."""

import asyncio
import re
from types import SimpleNamespace

import brand_graph as bg
import email_checks as ec
import email_doc
import httpx
import product_graph as pg
import pytest

BRAND = {
    "id": "acme-com",
    "nodes": [
        {"id": "company", "type": "Company", "name": "Acme", "legal_name": "Acme Inc."},
        {"id": "address", "type": "Address", "text": "1 Main St, Springfield"},
        {"id": "link:website", "type": "Link", "kind": "website", "url": "https://www.acme.example"},
        {"id": "link:shop", "type": "Link", "kind": "shop", "url": "https://acme.example/shop"},
        {"id": "social:instagram", "type": "SocialProfile", "network": "instagram", "url": "https://instagram.com/acme"},
    ],
    "edges": [],
}
PRODUCT = {"id": "tree-runner", "brand_id": "acme-com", "url": "https://acme.example/tree-runner", "nodes": [], "edges": []}


def spec(**overrides):
    return SimpleNamespace(**{"name": "Acme", "background": "#FFFFFF", "text": "#111111",
                              "font_stack": "Helvetica, Arial, sans-serif", **overrides})


def ctx(**kw):
    return ec.Context(brand_spec=kw.pop("brand_spec", spec()), log=lambda m: None, **kw)


def run(coro):
    return asyncio.run(coro)


# ── run_all ───────────────────────────────────────────────────────────────────


def test_one_failing_check_never_costs_the_email(monkeypatch):
    names = ["balance_columns", "check_facts", "ensure_footer", "check_links", "fix_alt_text", "check_logo",
             "fix_contrast", "check_dark_mode", "check_clients", "check_inbox"]
    for name in names:
        async def fake(html, c, name=name):
            if name == "check_links":
                raise RuntimeError("boom")
            return html + f"[{name}]", [ec.item(name, "pass", "ok")]
        monkeypatch.setattr(ec, name, fake)

    html, report = run(ec.run_all("<p>email</p>", ctx()))
    assert html == "<p>email</p>" + "".join(f"[{n}]" for n in names if n != "check_links")
    assert len(report) == len(names)
    assert ec.item("Links", "skipped", "check failed: RuntimeError: boom") in report


# ── facts ─────────────────────────────────────────────────────────────────────

EMAIL = """<!-- Section:Start hero --><table><tr><td><h1>Meet the Tree Runner</h1>
<p>Made from 100% recycled steel. Free shipping forever.</p></td></tr></table><!-- Section:End hero -->"""


def product_values(graph):
    return {"name": "Tree Runner", "price": "$98", "description": "A light sneaker", "features": ["Merino wool upper"],
            "variants": ["Natural White"], "images": [{"description": "side view"}], "branches": {}}


def test_facts_are_skipped_without_a_product_or_when_turned_off():
    assert run(ec.check_facts(EMAIL, ctx(fact_check=False)))[1][0]["status"] == "skipped"
    assert "no product picked" in run(ec.check_facts(EMAIL, ctx()))[1][0]["detail"]


def test_unsupported_claims_are_rewritten_or_flagged(monkeypatch):
    prompts = []

    async def ask(system, blocks, max_tokens):
        prompts.append(blocks[0]["text"])
        return {"issues": [
            {"quote": "Made from 100% recycled steel.", "problem": "material not on the page", "fix": "Made with a merino wool upper."},
            {"quote": "Lifetime warranty", "problem": "not in the email", "fix": ""},
            {"problem": "no quote, ignored"},
        ]}

    monkeypatch.setattr(pg, "values", product_values)
    monkeypatch.setattr(bg, "_ask_model", ask)
    html, items = run(ec.check_facts(EMAIL, ctx(brand=BRAND, product=PRODUCT, brief="Spring launch, $98")))
    assert "Made with a merino wool upper." in html and "recycled steel" not in html
    assert [i["status"] for i in items] == ["fixed", "warn"]
    assert "Merino wool upper" in prompts[0] and "Spring launch, $98" in prompts[0] and "Meet the Tree Runner" in prompts[0]


def test_facts_pass_when_the_model_finds_nothing(monkeypatch):
    async def ask(system, blocks, max_tokens):
        return {"issues": []}

    monkeypatch.setattr(pg, "values", product_values)
    monkeypatch.setattr(bg, "_ask_model", ask)
    html, items = run(ec.check_facts(EMAIL, ctx(brand=BRAND, product=PRODUCT)))
    assert html == EMAIL and items[0]["status"] == "pass"


# ── footer ────────────────────────────────────────────────────────────────────


def test_existing_footer_passes_and_a_missing_address_is_flagged():
    with_address = "<p>Acme Inc., 1 Main St, Springfield</p><a href='{{unsubscribe_link}}'>Unsubscribe</a>"
    assert run(ec.ensure_footer(with_address, ctx(brand=BRAND)))[1][0]["status"] == "pass"
    status, detail = (lambda i: (i["status"], i["detail"]))(run(ec.ensure_footer("<a>Unsubscribe</a>", ctx(brand=BRAND)))[1][0])
    assert status == "warn" and "postal address" in detail


@pytest.fixture
def compiled(monkeypatch):
    """Capture the footer MJML instead of running the mjml CLI."""
    seen = {}

    async def compile_mjml(mjml, font_stack):
        seen["mjml"] = mjml
        return "<tr><td>FOOTER</td></tr>"

    monkeypatch.setattr(email_doc, "compile_mjml_sections", compile_mjml)
    monkeypatch.setattr(email_doc, "insert_after_sections", lambda html, after, block: html + block)
    return seen


def test_a_missing_footer_is_added_with_sender_socials_and_unsubscribe(compiled):
    html, items = run(ec.ensure_footer("<p>Hello</p>", ctx(brand=BRAND)))
    mjml = compiled["mjml"]
    assert html.endswith("FOOTER</td></tr>")
    assert ec.UNSUBSCRIBE_TAG in mjml and "Acme Inc., 1 Main St, Springfield" in mjml
    assert ">acme.example</a>" in mjml and ">Instagram</a>" in mjml
    assert items[0]["status"] == "fixed" and "socials, address, website" in items[0]["detail"]


def test_footer_text_stays_readable_and_escaped(compiled):
    run(ec.ensure_footer("<p>Hello</p>", ctx(brand_spec=spec(name="A<b>", text="#EEEEEE"))))
    mjml = compiled["mjml"]
    assert "A&lt;b&gt;" in mjml and "A<b>" not in mjml
    text_colors = set(re.findall(r'(?<![-\w])color="(#[0-9A-F]{6})"', mjml))
    assert text_colors
    for color in text_colors:
        assert bg._contrast(color, "#FFFFFF") >= 4.5


def test_mix_and_muted():
    assert ec._mix("#000000", "#FFFFFF", 0.5) == "#808080"
    muted = ec._muted("#111111", "#FFFFFF")
    assert muted != "#111111" and bg._contrast(muted, "#FFFFFF") >= 4.5


# ── links ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("href", ["", "#", "/", "javascript:void(0)", "https://example.com/x", "https://acme.example/your-link", "https://"])
def test_placeholders(href):
    assert ec._is_placeholder(href)


def test_real_links_are_not_placeholders():
    assert not ec._is_placeholder("https://acme.example/shop")


def test_best_url_prefers_the_product_except_for_social_links():
    assert ec._best_url("Shop now", ctx(brand=BRAND, product=PRODUCT)) == PRODUCT["url"]
    assert ec._best_url("Follow us on Instagram", ctx(brand=BRAND, product=PRODUCT)) == "https://acme.example/shop"
    assert ec._best_url("Shop now", ctx()) is None


class FakeFetcher:
    """Answers each URL from a table: 'ok', an HTTP status code, or an exception to raise."""

    answers: dict = {}
    fetched: list = []
    closed = False

    def __init__(self, allow_host):
        FakeFetcher.fetched, FakeFetcher.closed = [], False

    async def get(self, url, max_bytes):
        FakeFetcher.fetched.append(url)
        answer = self.answers.get(url, "ok")
        if isinstance(answer, int):
            request = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("status", request=request, response=httpx.Response(answer, request=request))
        if isinstance(answer, Exception):
            raise answer

    async def close(self):
        FakeFetcher.closed = True


def test_links_are_checked_fixed_or_flagged(monkeypatch):
    FakeFetcher.answers = {
        "https://acme.example/old": 404,
        "https://partner.example/": 403,
        "https://instagram.com/gone": 404,
        "https://acme.example/video.mp4": ValueError("response larger than 50000000 bytes"),
    }
    monkeypatch.setattr(bg, "Fetcher", FakeFetcher)
    html = """<a href="https://example.com/your-link">Shop now</a> <a href="https://acme.example/old">Read more</a>
<a href="https://partner.example/">Partner</a> <a href="https://instagram.com/gone">Instagram</a>
<a href="https://acme.example/video.mp4">Watch</a> <a href="mailto:hi@acme.example">Email us</a> <a href="{{unsubscribe_link}}">Unsubscribe</a>"""
    out, items = run(ec.check_links(html, ctx(brand=BRAND, product=PRODUCT)))

    assert 'href="https://example.com/your-link"' not in out and 'href="https://acme.example/old"' not in out
    assert out.count(f'href="{PRODUCT["url"]}"') == 2
    assert 'href="https://instagram.com/gone"' in out  # a social profile is never swapped for a shop page
    assert [(i["status"], i["detail"].split(" ")[0]) for i in items] == [("fixed", "“Shop"), ("fixed", "“Read"), ("warn", "social")]
    assert not any(u.startswith(("mailto:", "{{")) for u in FakeFetcher.fetched)
    assert FakeFetcher.closed


def test_links_pass_and_count_sites_that_block_checks(monkeypatch):
    FakeFetcher.answers = {"https://partner.example/": 429}
    monkeypatch.setattr(bg, "Fetcher", FakeFetcher)
    html = '<a href="https://acme.example/shop">Shop</a><a href="https://partner.example/">Partner</a>'
    _, items = run(ec.check_links(html, ctx(brand=BRAND)))
    assert items == [ec.item("Links", "pass", "1 link checked and loading; 1 on sites that block automated checks")]


# ── alt text ──────────────────────────────────────────────────────────────────


def test_alt_text_comes_from_photo_descriptions_and_the_logo():
    html = """<p>
<img src="http://127.0.0.1:8765/files/runs/x/hero.png" width="600">
<img src="http://127.0.0.1:8765/files/brands/acme/logo-primary.png" alt="logo-primary.png">
<img src="kept.png" alt="Kept as is">
<img src="unknown.png">
</p>"""
    out, items = run(ec.fix_alt_text(html, ctx(image_descriptions={"runs/x/hero.png": 'Shoe "on" foot <3'})))
    assert '<img alt="Shoe &quot;on&quot; foot &lt;3" src="http://127.0.0.1:8765/files/runs/x/hero.png"' in out
    assert '<img alt="Acme logo" src="http://127.0.0.1:8765/files/brands/acme/logo-primary.png">' in out
    assert 'alt="Kept as is"' in out and '<img src="unknown.png">' in out
    assert items == [ec.item("Alt text", "fixed", "added alt text to 2 images; 1 still without")]


def test_alt_text_pass_and_warn():
    assert run(ec.fix_alt_text('<img src="a.png" alt="A">', ctx()))[1][0]["status"] == "pass"
    assert run(ec.fix_alt_text('<img src="a.png">', ctx()))[1][0]["detail"] == "1 image without alt text"


# ── inbox ─────────────────────────────────────────────────────────────────────


def test_inbox_pass():
    _, items = run(ec.check_inbox("<p>" + "Plain words. " * 30 + "</p>", ctx(subject_lines=[{"subject": "Meet the Tree Runner"}])))
    assert items == [ec.item("Inbox", "pass", "HTML is 0 KB (Gmail clips over 102 KB); no spam phrases; subject lengths fine")]


def test_inbox_warnings(storage):
    (storage / "big.jpg").write_bytes(b"0" * 1_600_000)
    html = "<p>" + "x" * 110_000 + "</p>" + '<img src="http://127.0.0.1:8765/files/big.jpg">'
    subjects = [{"subject": "AMAZING deal, act now", "preheader": ""}, {"subject": "a" * 30 + " " + "b" * 31}]
    details = [i["detail"] for i in run(ec.check_inbox(html, ctx(subject_lines=subjects)))[1]]
    assert re.match(r"HTML is 10\d KB; Gmail clips emails over 102 KB", details[0])
    assert details[1].startswith("1 images totalling 1.6 MB")
    assert "spam-trigger phrases: “act now”" in details
    assert "ALL-CAPS words in subject lines: AMAZING" in details
    assert "1 subject line is over 60 characters and will be cut off on phones" in details


def test_image_heavy_emails_are_flagged():
    html = '<img src="a.png"><img src="b.png"><img src="c.png"><p>Shop now</p>'
    assert any("mostly images (3)" in i["detail"] for i in run(ec.check_inbox(html, ctx()))[1])


# ── layout and style helpers ──────────────────────────────────────────────────


def test_rgb():
    assert ec._rgb("rgb(255, 0, 10)") == "#FF000A"
    assert ec._rgb("rgba(0, 0, 0, 0.2)") is None
    assert ec._rgb("transparent") is None


def test_set_style_replaces_or_adds_the_property():
    assert ec._set_style('<div class="a" style="color:red;vertical-align:middle">', "vertical-align", "top") == \
        '<div class="a" style="color:red;vertical-align:top !important">'
    assert ec._set_style('<div class="a">', "vertical-align", "top") == '<div style="vertical-align:top !important" class="a">'


def test_align_top_fixes_the_columns_and_their_outlook_cells():
    html = """<!--[if mso | IE]><td class="" style="vertical-align:middle;width:300px;" ><![endif]-->
<div class="mj-column-per-50 mj-outlook-group-fix" style="display:inline-block;vertical-align:middle;width:100%;">photo</div>
<!--[if mso | IE]></td><td class="" style="vertical-align:middle;width:300px;" ><![endif]-->
<div class="mj-column-per-50 mj-outlook-group-fix" style="display:inline-block;vertical-align:middle;width:100%;">copy</div>
<!--[if mso | IE]></td><![endif]-->"""
    out = ec._align_top(html, [0, 1, 7])
    assert "vertical-align:middle" not in out
    assert out.count("vertical-align:top !important") == 2 and out.count('style="vertical-align:top;width:300px;"') == 2


# ── email clients ─────────────────────────────────────────────────────────────


def test_client_features_skip_hidden_and_outlook_only_code():
    html = """<div style="backdrop-filter: blur(4px)">Hi</div>
<div style="display:none"><span style="animation: spin 1s">preheader</span></div>
<!--[if mso]><div style="border-image: url(x.png) 30"></div><![endif]-->"""
    found = {c["id"] for c in ec.client_features(html)}
    assert "css-backdrop-filter" in found
    assert not found & {"css-animation", "css-border-image"}
    assert run(ec.check_clients(html, ctx()))[1][0]["status"] == "warn"


def test_plain_email_code_passes_the_client_check():
    assert run(ec.check_clients("<table><tr><td style='color:#111111'>Hi</td></tr></table>", ctx()))[1][0]["status"] == "pass"
