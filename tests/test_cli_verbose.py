from __future__ import annotations

import json
import sys
import subprocess
from pathlib import Path

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


def test_package_main_can_be_executed_directly_like_vscode():
    project_root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,str(project_root/"bistbot"/"main.py"),"--help"],
        cwd=project_root/"bistbot",capture_output=True,text=True,timeout=15)
    assert result.returncode==0
    assert "BISTBOT V1 paper trader" in result.stdout


def test_verbose_formatter_prints_scores_hold_reason_and_risk(capsys):
    cli.print_verbose_diagnostics(DIAGNOSTICS); output=capsys.readouterr().out
    assert "TOP 40 SCANNER CANDIDATES" in output and "trend_score=79.00" in output
    assert "TOP 10 INTELLIGENCE / LLM CANDIDATES" in output and "priced_in_probability: 45" in output
    assert "HOLD REASON" in output and "final_score 76.4 below buy_threshold 78" in output
    assert "RISK DECISION" in output and "reason_code: MAX_POSITION_SIZE" in output


def test_verbose_formatter_prints_dynamic_entry_plan_and_explicit_rr_rejection(capsys):
    diagnostics={"candidates":[{"symbol":"AAA","scanner_score":80,"decision":"HOLD",
        "reason":"INSUFFICIENT_RISK_REWARD","news_status":"NO_NEWS","llm_status":"NOT_REQUIRED",
        "signal_mode":"TECHNICAL_ONLY","entry_plan":{"entry_price":"100","initial_stop_price":"95",
            "target_1":"103","target_2":"106","target_3":"109","expected_upside_pct":6,
            "downside_risk_pct":5,"risk_reward_ratio":1.2,"potential_score":58,"holding_horizon":"SHORT_SWING",
            "target_confidence":"MEDIUM","target_method":"STRUCTURE_ATR","target_components":{
                "resistance_target":"106","swing_high_target":"109","atr_target":"105",
                "trend_extension_target":"107","catalyst_adjustment":0}}}]}
    cli.print_verbose_diagnostics(diagnostics); output=capsys.readouterr().out
    assert "DYNAMIC ENTRY PLAN" in output and "target_3: 109" in output
    assert "TARGET COMPONENTS" in output and "resistance_target: 106" in output
    assert "DECISION:\nHOLD\nreason: INSUFFICIENT_RISK_REWARD" in output


def test_cycle_summary_explains_raw_buy_blocked_by_existing_position(capsys):
    diagnostics={"candidates":[{"symbol":"GUBRF.IS","final_score":72.509,"strategy_decision":"BUY",
        "decision":"HOLD","reason":"existing paper position; pyramiding disabled"}]}
    summary={"market_data_success":625,"symbols_valid":648,"scanner_candidates":40,"llm_candidates":10,
        "llm_api_attempts":0,"raw_buy_signals":1,"buy_signals":0,"entry_orders":0,"exit_orders":0}
    cli.print_cycle_action_summary(summary,diagnostics); output=capsys.readouterr().out
    assert "strategy=BUY  action=MANAGE_EXISTING_POSITION" in output
    assert "post-strategy portfolio/risk rules blocked" in output


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
