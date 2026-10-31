"""Speed modes: which model each step gets, for CLI logins and for API keys."""

import asyncio

import model_routing as mr
import pytest

PLANNER = "You are a **senior consumer-brand copywriter** with 15+ years of experience..."
TEMPLATE_PICKER = "You are an expert email template selector for consumer-brand marketing emails."


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("SPEED_MODE", raising=False)
    monkeypatch.setattr(mr, "FORCED_API_MODEL", "")
    mr.STATS.clear()


@pytest.mark.parametrize(("value", "expected"), [(None, "best"), ("balanced", "balanced"), (" FAST ", "fast"), ("turbo", "best")])
def test_mode_reads_speed_mode(monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("SPEED_MODE", value)
    assert mr.mode() == expected


@pytest.mark.parametrize(
    ("text", "step"),
    [
        (TEMPLATE_PICKER, "template picker"),
        ("You are a brand-consistency art director for consumer brands.", "rule mapper"),
        ("You are curating brand-library imagery for a consumer-brand marketing email.", "image shortlist"),
        ("You are doing a focused asset-restraint pass on the rendered CONTENT SECTIONS", "asset pruner"),
        ("You are an expert MJML (Mailjet Markup Language) validator.", "mjml validator"),
        ("generate 5 different versions of email topper (subject line and preheader)", "subject lines"),
        (PLANNER, None),
    ],
)
def test_light_step_recognises_mechanical_prompts(text, step):
    assert mr.light_step(text) == step


def test_light_step_only_reads_the_opening():
    assert mr.light_step("x" * 6000 + TEMPLATE_PICKER) is None


def test_best_keeps_every_model(monkeypatch):
    monkeypatch.setenv("SPEED_MODE", "best")
    assert mr._route("claude-opus-4-1", TEMPLATE_PICKER, cli=False) == "claude-opus-4-1"
    assert mr._route("opus", TEMPLATE_PICKER, cli=True) == "opus"


def test_balanced_moves_only_mechanical_steps(monkeypatch):
    monkeypatch.setenv("SPEED_MODE", "balanced")
    assert mr._route("claude-opus-4-1", TEMPLATE_PICKER, cli=False) == mr.FAST_API_MODEL
    assert mr._route("opus", TEMPLATE_PICKER, cli=True) == "sonnet"
    assert mr._route("claude-opus-4-1", PLANNER, cli=False) == "claude-opus-4-1"
    assert mr.STATS["template picker"] == 2


def test_fast_moves_every_claude_step(monkeypatch):
    monkeypatch.setenv("SPEED_MODE", "fast")
    assert mr._route("claude-opus-4-1", PLANNER, cli=False) == mr.FAST_API_MODEL
    assert mr._route("opus", PLANNER, cli=True) == "sonnet"
    assert mr.STATS["fast"] == 2


def test_api_mode_never_touches_other_providers(monkeypatch):
    monkeypatch.setenv("SPEED_MODE", "fast")
    assert mr._route("gpt-5.5", PLANNER, cli=False) == "gpt-5.5"


def test_a_model_picked_under_model_access_beats_speed(monkeypatch):
    monkeypatch.setenv("SPEED_MODE", "fast")
    monkeypatch.setattr(mr, "FORCED_API_MODEL", "claude-haiku-4-5")
    assert mr._route("claude-opus-4-1", PLANNER, cli=False) == "claude-haiku-4-5"
    assert mr._route("gpt-5.5", PLANNER, cli=False) == "gpt-5.5"


class FakeBackends:
    """The two entry points llm_backends exposes; they echo the model they were asked for."""

    def run_sync(self, system, blocks, model_hint, effort=None):
        return model_hint

    async def run_async(self, system, blocks, model_hint, effort=None):
        return model_hint


def test_install_routes_cli_calls_in_fast_mode(monkeypatch):
    monkeypatch.setenv("SPEED_MODE", "fast")
    backends = FakeBackends()
    mr.install(backends)
    assert backends.run_sync(PLANNER, [], "opus") == "sonnet"
    assert asyncio.run(backends.run_async(PLANNER, [], "opus")) == "sonnet"


def test_install_does_nothing_in_best_mode():
    backends = FakeBackends()
    original = backends.run_sync
    mr.install(backends)
    assert backends.run_sync == original


def test_wrap_rewrites_the_model_argument_of_sync_and_async_calls(monkeypatch):
    monkeypatch.setenv("SPEED_MODE", "fast")

    def create(**kw):
        return kw["model"]

    async def acreate(**kw):
        return kw["model"]

    sync, asyn = mr._wrap(create), mr._wrap(acreate)
    assert sync(model="claude-opus-4-1", system=PLANNER, messages=[]) == mr.FAST_API_MODEL
    assert asyncio.run(asyn(model="claude-opus-4-1", system=PLANNER, messages=[])) == mr.FAST_API_MODEL
    assert sync._model_routing and asyn._model_routing


def test_wrap_passes_calls_without_a_model_through(monkeypatch):
    monkeypatch.setenv("SPEED_MODE", "fast")
    assert mr._wrap(lambda **kw: kw)(system=PLANNER, max_tokens=10) == {"system": PLANNER, "max_tokens": 10}
