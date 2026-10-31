"""A model API is reachable only when its key is set."""

import egress
import pytest

ANTHROPIC, OPENAI, GOOGLE = "api.anthropic.com", "api.openai.com", "generativelanguage.googleapis.com"


@pytest.mark.parametrize(
    ("environ", "hosts"),
    [
        ({}, set()),
        ({"ANTHROPIC_API_KEY": "unset", "OPENAI_API_KEY": "unset", "GOOGLE_API_KEY": "unset"}, set()),
        ({"ANTHROPIC_API_KEY": "", "GOOGLE_API_KEY": ""}, set()),
        ({"ANTHROPIC_API_KEY": "sk-ant-test"}, {ANTHROPIC}),
        ({"OPENAI_API_KEY": "sk-test", "GOOGLE_API_KEY": "unset"}, {OPENAI}),
        ({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "b", "GOOGLE_API_KEY": "c"}, {ANTHROPIC, OPENAI, GOOGLE}),
    ],
)
def test_model_api_hosts_follow_the_keys(environ, hosts):
    assert egress.model_api_hosts(environ) == hosts


def test_a_login_run_without_a_google_key_cannot_reach_gemini():
    # The case found on a real run: Claude Code login, no Google key, a Gemini backup step.
    assert GOOGLE not in egress.model_api_hosts({"ANTHROPIC_API_KEY": "unset", "GOOGLE_API_KEY": ""})
