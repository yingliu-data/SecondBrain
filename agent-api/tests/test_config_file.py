"""The config file data/config.json (plan feat/52 step 1, issue #52).

The loader runs on a temp file with a patched env; the settings that
app.config sets at import are read in a subprocess, so no test reloads
app.config under the others.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import llm_config, load_config_file

AGENT_API = Path(__file__).resolve().parent.parent
TODAY = ("vllm", "qwen3-14b", "http://secondbrain-llm:8080", "")


@pytest.fixture(autouse=True)
def _no_llm_env(monkeypatch):
    for name in ("LLM_VENDOR", "LLM_MODEL", "LLM_URL", "LLM_API_KEY_ENV",
                 "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _write(tmp_path, doc) -> str:
    path = tmp_path / "config.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))
    return str(path)


def _import_config(config_file: str, **env) -> dict:
    """app.config's values, imported in a fresh interpreter."""
    code = ("import json; from app import config; print(json.dumps({'max_turns': config.HARNESS_MAX_TURNS, "
            "'stream': config.LLM_STREAM, 'temperature': config.LLM_TEMPERATURE, 'vendor': config.LLM_VENDOR}))")
    out = subprocess.run([sys.executable, "-c", code], cwd=AGENT_API, capture_output=True, text=True, check=True,
                         env={**os.environ, "CONFIG_FILE": config_file, **env})
    return json.loads(out.stdout)


def test_no_file_gives_todays_values(tmp_path):
    """Plan criterion: with no config file, behaviour is the same as today (local vLLM)."""
    assert load_config_file(str(tmp_path / "missing.json")) == {}
    assert llm_config({}) == TODAY


def test_llm_block_is_read(tmp_path, monkeypatch):
    """Plan criterion: the file holds the vendor, its model and URL; the template gives today's values."""
    template = load_config_file(str(AGENT_API / "config.json.example"))
    assert llm_config(template["llm"]) == TODAY

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    doc = load_config_file(_write(tmp_path, {"llm": {"vendor": "gemini", "model": "gemini-2.5-flash"}}))
    assert llm_config(doc["llm"]) == (
        "gemini", "gemini-2.5-flash", "https://generativelanguage.googleapis.com", "GEMINI_API_KEY")


def test_settings_block_sets_config_values(tmp_path):
    """Plan criterion: the file holds the other settings now read from env vars (JSON values and strings)."""
    path = _write(tmp_path, {"settings": {"HARNESS_MAX_TURNS": 3, "LLM_STREAM": False, "LLM_TEMPERATURE": "0.2"}})
    assert _import_config(path) == {"max_turns": 3, "stream": False, "temperature": 0.2, "vendor": "vllm"}


def test_env_overrides_file(tmp_path, monkeypatch):
    """Plan criterion: environment variables still override the file."""
    path = _write(tmp_path, {"llm": {"vendor": "openai", "model": "gpt-x", "url": "http://file"},
                             "settings": {"HARNESS_MAX_TURNS": 3}})
    monkeypatch.setenv("LLM_VENDOR", "vllm")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.setenv("LLM_URL", "http://env")
    assert llm_config(load_config_file(path)["llm"]) == ("vllm", "env-model", "http://env", "")
    assert _import_config(path, HARNESS_MAX_TURNS="7")["max_turns"] == 7


@pytest.mark.parametrize("doc, field", [
    ({"llm": {"vendor": "openai", "api_key": "sk-x"}}, "llm.api_key"),
    ({"settings": {"API_SECRET_KEY": "x"}}, "settings.API_SECRET_KEY"),
    ({"settings": {"INTERNAL_TOKEN": "x"}}, "settings.INTERNAL_TOKEN"),
    ({"settings": {"OPENAI_API_KEY": "x"}}, "settings.OPENAI_API_KEY"),
])
def test_key_in_file_is_refused(tmp_path, doc, field):
    """Plan criterion: no API key is written in the file; the error names the field."""
    with pytest.raises(ValueError, match=field):
        load_config_file(_write(tmp_path, doc))


@pytest.mark.parametrize("value", [None, ""])
def test_missing_key_env_names_it(monkeypatch, value):
    """Plan criterion: the chosen vendor's key env var is unset or empty -> an error naming it."""
    if value is not None:
        monkeypatch.setenv("OPENAI_API_KEY", value)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        llm_config({"vendor": "openai", "model": "gpt-x"})


@pytest.mark.parametrize("block, field", [
    ({"vendor": "foo"}, "llm.vendor"),
    ({"vendor": "anthropic"}, "llm.model"),
])
def test_unknown_vendor_names_field(block, field):
    """Plan criterion: an unknown vendor (or a missing model) -> an error naming the field."""
    with pytest.raises(ValueError, match=field):
        llm_config(block)


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", '{"llm": "vllm"}'])
def test_invalid_json_names_file(tmp_path, text):
    """Plan criterion: a config file that is not valid -> an error naming the file; the import fails."""
    path = _write(tmp_path, text)
    with pytest.raises(ValueError, match=path):
        load_config_file(path)
    with pytest.raises(subprocess.CalledProcessError) as e:
        _import_config(path)
    assert path in e.value.stderr
