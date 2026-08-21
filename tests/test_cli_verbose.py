from __future__ import annotations

import json
import sys

import yaml

import bistbot.main as cli


DIAGNOSTICS={
    "scanner":[{"rank":1,"symbol":"AAA","scanner_score":88.1,"technical_score":80,
        "momentum_score":82,"volume_score":75,"trend_score":79,"liquidity_score":90,
        "latest_price":100,"latest_timestamp":"2026-08-21T12:00:00+00:00","average_volume":200000,
        "latest_volume":300000,"relative_volume":1.5,"ema9":99,"ema21":97,"rsi14":60,"atr14":2,
        "average_turnover_try":20000000,"trading_continuity":1}],
    "candidates":[{"symbol":"AAA","scanner_score":88.1,"news_kap_score":70,"sentiment":20,
        "importance":60,"catalyst_score":55,"priced_in_probability":45,"risk_score":30,
        "confidence":35,"action_bias":"HOLD","final_score":76.4,"decision":"HOLD",
        "reason":"final_score 76.4 below buy_threshold 78","news_status":"NO_NEWS",
        "llm_status":"NOT_REQUIRED","signal_mode":"TECHNICAL_ONLY"}],
    "risk":[{"symbol":"BBB","proposed_position_value":"25000.00","proposed_quantity":250,
        "stop_price":"96.00","maximum_allowed_risk":"1000.00","risk_decision":"REDUCE_SIZE",
        "reason_code":"MAX_POSITION_SIZE","reason":"Requested quantity reduced"}],
}


def test_parser_accepts_verbose_with_once():
    args=cli.build_parser().parse_args(["--capital","200000","--once","--verbose"])
    assert args.once and args.verbose and args.capital==200000


def test_verbose_formatter_prints_scores_hold_reason_and_risk(capsys):
    cli.print_verbose_diagnostics(DIAGNOSTICS); output=capsys.readouterr().out
    assert "TOP 40 SCANNER CANDIDATES" in output and "trend_score=79.00" in output
    assert "TOP 10 INTELLIGENCE / LLM CANDIDATES" in output and "priced_in_probability: 45" in output
    assert "HOLD REASON" in output and "final_score 76.4 below buy_threshold 78" in output
    assert "RISK DECISION" in output and "reason_code: MAX_POSITION_SIZE" in output


def test_once_verbose_keeps_json_summary_as_final_output(tmp_path,monkeypatch,capsys):
    raw=yaml.safe_load(open("config.v1.yaml",encoding="utf-8")); raw["database"]=str(tmp_path/"cli.db")
    config=tmp_path/"config.yaml"; config.write_text(yaml.safe_dump(raw),encoding="utf-8")
    summary={"symbols":523,"scanner_candidates":40,"llm_candidates":10,"holds":10}

    class FakeApplication:
        def __init__(self,*args,**kwargs): self.last_diagnostics=DIAGNOSTICS
        def run_cycle(self,**kwargs): return summary
        def provider_status(self): return ["MarketDataProvider: Fake [MOCK]"]

    monkeypatch.setattr(cli,"BistBotApplication",FakeApplication)
    monkeypatch.setattr(sys,"argv",["main.py","--config",str(config),"--capital","200000","--once","--verbose"])
    assert cli.main()==0
    output=capsys.readouterr().out.rstrip()
    assert "TOP 40 SCANNER CANDIDATES" in output
    assert output.endswith(json.dumps(summary,indent=2))
