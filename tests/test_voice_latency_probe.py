"""Offline probe case selection; no credentials or network used."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def probe_module():
    path = Path(__file__).parents[1] / "tools" / "voice_latency_probe.py"
    spec = importlib.util.spec_from_file_location("voice_latency_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("case", ["greeting", "fact", "calculation"])
def test_named_cases(probe_module, case):
    args = probe_module.parse_args(["--case", case])
    assert args.case == case
    assert probe_module.PROMPTS[case]


def test_default_avoids_calculation_and_retains_old_fixture(probe_module):
    assert probe_module.parse_args([]).case == "greeting"
    assert probe_module.PROMPTS["calculation"] == probe_module.PROMPT


def test_unknown_case_rejected(probe_module):
    with pytest.raises(SystemExit):
        probe_module.parse_args(["--case", "unknown"])
