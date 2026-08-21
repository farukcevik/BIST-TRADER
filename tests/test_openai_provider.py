from __future__ import annotations

import json
from types import SimpleNamespace

import yaml

import bistbot.main as cli
from bistbot.app.config import load_settings
from bistbot.intelligence.openai_provider import OpenAILLMProvider


class FakeResponses:
    def __init__(self, output_text: str):
        self.output_text = output_text
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=self.output_text,
            model="configured-model",
            usage=SimpleNamespace(input_tokens=101, output_tokens=37, total_tokens=138),
        )


def test_openai_provider_uses_responses_structured_output_and_records_usage():
    responses=FakeResponses('{"symbol":"THYAO.IS"}')
    provider=OpenAILLMProvider(model="configured-model",api_key="test-key",client=SimpleNamespace(responses=responses))
    completion=provider.complete(system_prompt="system",input_json='{"symbol":"THYAO.IS"}')

    assert completion.content=='{"symbol":"THYAO.IS"}'
    assert (completion.input_tokens,completion.output_tokens,completion.total_tokens)==(101,37,138)
    request=responses.calls[0]
    assert request["model"]=="configured-model"
    assert request["instructions"]=="system" and request["input"]=='{"symbol":"THYAO.IS"}'
    schema=request["text"]["format"]
    assert schema["type"]=="json_schema" and schema["strict"] is True
    assert set(schema["schema"]["required"])==set(schema["schema"]["properties"])


def test_llm_config_defaults_disabled_and_loads_openai(tmp_path):
    raw=yaml.safe_load(open("config.v1.yaml",encoding="utf-8"))
    config=tmp_path/"config.yaml"; config.write_text(yaml.safe_dump(raw),encoding="utf-8")
    assert load_settings(config).llm.enabled is False
    raw["llm"]={"provider":"openai","model":"configured-model","enabled":True,"timeout_seconds":12}
    config.write_text(yaml.safe_dump(raw),encoding="utf-8")
    settings=load_settings(config)
    assert settings.llm.provider=="openai" and settings.llm.model=="configured-model"
    assert settings.llm.timeout_seconds==12


def test_runtime_selects_openai_only_with_enabled_config_and_key(tmp_path,monkeypatch,capsys):
    raw=yaml.safe_load(open("config.v1.yaml",encoding="utf-8")); raw["database"]=str(tmp_path/"cli.db")
    raw["llm"]={"provider":"openai","model":"configured-model","enabled":True,"timeout_seconds":30}
    config=tmp_path/"config.yaml"; config.write_text(yaml.safe_dump(raw),encoding="utf-8")
    captured={}

    class FakeProvider:
        provider_mode="REAL"
        def __init__(self,**kwargs): captured.update(kwargs)

    class FakeApplication:
        def __init__(self,*args,**kwargs): captured["provider"]=kwargs["llm_provider"]
        def provider_status(self): return ["LLMProvider: FakeProvider [REAL]","LLMModel: configured-model"]
        def run_cycle(self,**kwargs): return {"ok":True}

    monkeypatch.setenv("OPENAI_API_KEY","secret-from-environment")
    monkeypatch.setattr(cli,"PROJECT_ENV_FILE",tmp_path/"missing.env")
    monkeypatch.setattr(cli,"OpenAILLMProvider",FakeProvider)
    monkeypatch.setattr(cli,"BistBotApplication",FakeApplication)
    monkeypatch.setattr("sys.argv",["main.py","--config",str(config),"--once"])
    assert cli.main()==0
    assert captured["api_key"]=="secret-from-environment" and captured["model"]=="configured-model"
    output=capsys.readouterr().out
    assert "OPENAI_API_KEY: configured" in output and "LLMModel: configured-model" in output
    assert "secret-from-environment" not in output


def test_runtime_disables_openai_without_api_key(tmp_path,monkeypatch):
    raw=yaml.safe_load(open("config.v1.yaml",encoding="utf-8")); raw["database"]=str(tmp_path/"cli.db")
    raw["llm"]={"provider":"openai","model":"configured-model","enabled":True,"timeout_seconds":30}
    config=tmp_path/"config.yaml"; config.write_text(yaml.safe_dump(raw),encoding="utf-8")
    captured={}

    class FakeApplication:
        def __init__(self,*args,**kwargs): captured.update(kwargs)
        def provider_status(self): return []
        def run_cycle(self,**kwargs): return {"ok":True}

    monkeypatch.delenv("OPENAI_API_KEY",raising=False)
    monkeypatch.setattr(cli,"PROJECT_ENV_FILE",tmp_path/"missing.env")
    monkeypatch.setattr(cli,"BistBotApplication",FakeApplication)
    monkeypatch.setattr("sys.argv",["main.py","--config",str(config),"--once"])
    assert cli.main()==0
    assert captured["llm_provider"] is None and captured["llm_model"]=="disabled-v1"


def test_project_env_file_loads_key_without_printing_it(tmp_path,monkeypatch,capsys):
    secret="test-secret-that-must-not-be-printed"
    env_file=tmp_path/".env"; env_file.write_text(f"OPENAI_API_KEY={secret}\n",encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY",raising=False)
    monkeypatch.setattr(cli,"PROJECT_ENV_FILE",env_file)
    assert cli.load_project_environment() is True
    assert cli.os.getenv("OPENAI_API_KEY")==secret
    assert secret not in capsys.readouterr().out


def test_disabled_llm_stays_disabled_when_key_is_present(tmp_path,monkeypatch):
    raw=yaml.safe_load(open("config.v1.yaml",encoding="utf-8")); raw["database"]=str(tmp_path/"cli.db")
    raw["llm"]={"provider":"openai","model":"configured-model","enabled":False,"timeout_seconds":30}
    config=tmp_path/"config.yaml"; config.write_text(yaml.safe_dump(raw),encoding="utf-8")
    captured={}

    class FakeApplication:
        def __init__(self,*args,**kwargs): captured.update(kwargs)
        def provider_status(self): return []
        def run_cycle(self,**kwargs): return {"ok":True}

    monkeypatch.setenv("OPENAI_API_KEY","present-but-disabled")
    monkeypatch.setattr(cli,"PROJECT_ENV_FILE",tmp_path/"missing.env")
    monkeypatch.setattr(cli,"BistBotApplication",FakeApplication)
    monkeypatch.setattr("sys.argv",["main.py","--config",str(config),"--once"])
    assert cli.main()==0
    assert captured["llm_provider"] is None
