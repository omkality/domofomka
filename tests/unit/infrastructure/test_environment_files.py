"""Общая раскладка окружений и сохранение существующих значений без вывода секретов."""

import io

import pytest
from dotenv import dotenv_values

from tools.configuration.environment import ASSIGNMENT, check_examples, normalize, template


def test_examples_have_the_same_explicit_keys_and_order():
    check_examples()
    assert ASSIGNMENT.findall(template("dev")) == ASSIGNMENT.findall(template("prod"))


def test_normalization_preserves_quotes_interpolation_and_unknown_settings():
    source = template("dev") + "\nADDITIONAL='literal ${OTHER} # value'\nMULTILINE=\"first\nsecond\"\n"
    normalized = normalize(source, "dev")
    before = dotenv_values(stream=io.StringIO(source), interpolate=False)
    after = dotenv_values(stream=io.StringIO(normalized), interpolate=False)
    assert all(after[key] == value for key, value in before.items())
    assert normalize(normalized, "dev") == normalized


def test_missing_optional_feature_keeps_runtime_default_not_example_preference():
    source = template("dev").replace("TELEMETRY_ENABLED=true\n", "")
    result = dotenv_values(stream=io.StringIO(normalize(source, "dev")))
    assert result["TELEMETRY_ENABLED"] == "false"


def test_missing_required_setting_is_never_replaced_by_an_example_secret():
    source = "\n".join(line for line in template("dev").splitlines() if not line.startswith("VK_GROUP_TOKEN="))
    with pytest.raises(ValueError, match="Missing required setting VK_GROUP_TOKEN"):
        normalize(source, "dev")
