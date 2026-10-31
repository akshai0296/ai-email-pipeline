"""The brief checklist (brief_writer.CHECKS) and the rewrite loop that fills its gaps."""

import asyncio

import brief_writer
import pytest

GOOD = (
    "Launch email for the new Tree Runner in three spring colors. Open with a hero photo of the shoe and a short headline, "
    "then a paragraph on the merino wool upper, then a grid with one image per color. "
    'Close with a "Shop the Tree Runner" button that goes to the product page.'
)


def by_name(text: str, grounded: bool = True) -> dict:
    return {c["name"]: c for c in brief_writer.checklist(text, grounded)}


def test_complete_brief_meets_every_check():
    assert all(c["ok"] for c in brief_writer.checklist(GOOD, grounded=True))
    assert brief_writer.missing(GOOD) == []


@pytest.mark.parametrize(
    ("failing", "text"),
    [
        ("Purpose", 'Open with a hero photo and a headline, then a paragraph about the wool upper, then close with a "Shop" button '
                    "that takes readers straight to the product page on the website today."),
        ("Layout", 'Launch email for our spring shoe with a photo of it and a "Shop now" button for readers who want to buy it '
                   "today from the website, written in the brand's usual friendly voice."),
        ("Images", 'Launch email. Open with a headline, then a paragraph on the wool upper, then close with a "Shop now" button '
                   "that goes to the product page, written in the brand's usual friendly voice."),
        ("CTA label", "Launch email. Open with a hero photo and a headline, then a paragraph on the wool upper, then close with a "
                      "Shop now button that goes to the product page on the website."),
    ],
)
def test_each_check_fails_alone_when_its_part_is_missing(failing, text):
    result = by_name(text)
    assert not result[failing]["ok"]
    assert result[failing]["tip"]
    assert [n for n, c in result.items() if not c["ok"]] == [failing]


@pytest.mark.parametrize(
    "cta",
    ['a "Shop now" button', "a “Shop now” button", "CTA: 'Shop now'", 'button labelled "Get yours"'],
)
def test_cta_label_accepts_straight_and_curly_quotes(cta):
    assert by_name(f"Launch email with a hero photo, then {cta}.")["CTA label"]["ok"]


def test_cta_label_needs_quotes_around_the_label():
    assert not by_name("Launch email with a hero photo, then a Shop now button.")["CTA label"]["ok"]


def test_length_bounds_and_tips():
    short = by_name("Launch the shoe.")["Length"]
    assert not short["ok"] and short["tip"] == f"add detail ({brief_writer.LENGTH[0]}+ words)"
    long = by_name(GOOD + " more" * 230)["Length"]
    assert not long["ok"] and long["tip"].startswith("trim it")
    assert by_name(GOOD)["Length"]["ok"]


def test_grounded_comes_from_picking_a_company_not_the_text():
    assert not by_name(GOOD, grounded=False)["Grounded"]["ok"]
    assert all(c["name"] != "Grounded" for c in brief_writer.missing("Launch the shoe."))


class FakeWriter:
    """Stands in for the model call: returns the queued briefs and records each prompt."""

    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    async def __call__(self, system, prompt, tag="brief"):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else []


def test_complete_leaves_a_finished_brief_alone(monkeypatch):
    fake = FakeWriter()
    monkeypatch.setattr(brief_writer, "_ask_brief", fake)
    assert asyncio.run(brief_writer.complete(GOOD)) == GOOD
    assert fake.prompts == []


def test_complete_asks_for_exactly_what_is_missing(monkeypatch):
    fake = FakeWriter([GOOD])
    monkeypatch.setattr(brief_writer, "_ask_brief", fake)
    draft = "Launch email for the Tree Runner with a photo of the shoe."
    assert asyncio.run(brief_writer.complete(draft, context="Tree Runner: merino wool upper")) == GOOD
    (prompt,) = fake.prompts
    assert draft in prompt and "merino wool upper" in prompt
    tips = {c["name"]: c["tip"] for c in brief_writer.checklist(draft, True)}
    assert tips["Layout"] in prompt and tips["CTA label"] in prompt
    assert tips["Images"] not in prompt  # already met, so not asked for


def test_complete_keeps_the_draft_when_the_model_returns_nothing(monkeypatch):
    fake = FakeWriter()
    monkeypatch.setattr(brief_writer, "_ask_brief", fake)
    assert asyncio.run(brief_writer.complete("Launch the shoe.", attempts=2)) == "Launch the shoe."
    assert len(fake.prompts) == 2
