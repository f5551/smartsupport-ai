from decimal import Decimal

import pytest

from config import AgentConfig


@pytest.mark.parametrize("overrides", [
    {"model": ""}, {"max_turns": 9}, {"max_turns": True},
    {"budget_usd": Decimal("0.021")}, {"budget_usd": Decimal("NaN")},
    {"permission_policy": "always_allow"}, {"max_retries": 2},
    {"timeout_seconds": 0}, {"timeout_seconds": 61},
    {"max_output_tokens": 0}, {"max_output_tokens": 2049},
    {"input_price": Decimal("Infinity")}, {"output_price": Decimal("-1")},
])
def test_unsafe_configuration_is_rejected(overrides):
    with pytest.raises(ValueError):
        AgentConfig(**{"model": "configured-model", **overrides})


def test_default_capstone_limits():
    config = AgentConfig(model="configured-model")
    assert config.max_turns == 4
    assert config.budget_usd == Decimal("0.005")
    assert config.permission_policy == "ask_on_write"
    assert config.input_price is None
