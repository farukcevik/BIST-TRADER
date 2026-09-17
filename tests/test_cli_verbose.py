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
        "llm_status":"NOT_REQUIRED","signal_mode":"TECHNICAL_ONLY",
        "fundamental":{"fundamental_score":100,"effective_score":48.73,"coverage":38.46,
            "confidence":49.42,"score_breakdown":{"profile":"GENERAL","metric_values":{
                "revenue_yoy":12.5,"gross_margin":.31,"net_margin":None,"roe":.18,
                "ocf_net_income":1.1,"equity_assets":.42,"asset_yoy":8.4}}}}],
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
    assert "raw_score: 100" in output and "effective_score: 48.73" in output
    assert "coverage: 38.46" in output and "confidence: 49.42" in output
    assert "Profile: GENERAL" in output and "Revenue YoY: 12.5" in output
    assert "Net Margin: UNKNOWN" in output and "OCF / Net Income: 1.1" in output
    assert "EBITDA Growth:" not in output and "Net Debt / EBITDA:" not in output
    assert "HOLD REASON" in output and "final_score 76.4 below buy_threshold 78" in output
    assert "RISK DECISION" in output and "reason_code: MAX_POSITION_SIZE" in output


def test_verbose_formatter_prints_valid_multi_target_potential(capsys):
    diagnostics={"candidates":[{"symbol":"AAA","decision":"HOLD","final_score":70,"reason":"fixture",
        "news_status":"NO_NEWS","llm_status":"NOT_REQUIRED","signal_mode":"STAGED_INVESTMENT",
        "latest_price":100,"potential":{"available":True,"downside_reference":96,
            "downside_risk_pct":4,"target_1":104,"target_2":108,"target_3":112,
            "rr_t1":1,"rr_t2":2,"rr_t3":3,"entry_rr":1.5}}]}

    cli.print_verbose_diagnostics(diagnostics); output=capsys.readouterr().out

    assert "Potential: VALID" in output
    assert "Entry: 100" in output and "Dynamic Stop: 96" in output and "Dynamic Stop %: 4" in output
    assert "T1: 104" in output and "T2: 108" in output and "T3: 112" in output
    assert "RR_T1: 1" in output and "RR_T2: 2" in output and "RR_T3: 3" in output
    assert "Final entry_rr: 1.5" in output


def test_verbose_formatter_explains_rejected_potential(capsys):
    diagnostics={"candidates":[{"symbol":"AAA","decision":"HOLD","final_score":70,"reason":"fixture",
        "news_status":"NO_NEWS","llm_status":"NOT_REQUIRED","signal_mode":"STAGED_INVESTMENT",
        "latest_price":100,"potential":{"available":False,
            "unavailable_reason":"INSUFFICIENT_VALIDATED_TARGETS"}}]}

    cli.print_verbose_diagnostics(diagnostics); output=capsys.readouterr().out

    assert "Potential: REJECTED" in output
    assert "Potential Rejection Reason: INSUFFICIENT_VALIDATED_TARGETS" in output


def test_verbose_formatter_identifies_missing_potential_fields_without_reason(capsys):
    diagnostics={"candidates":[{"symbol":"AAA","decision":"HOLD","final_score":70,"reason":"fixture",
        "news_status":"NO_NEWS","llm_status":"NOT_REQUIRED","signal_mode":"STAGED_INVESTMENT",
        "latest_price":100,"potential":{"available":True,"downside_reference":96,"downside_risk_pct":4,
            "target_1":104,"rr_t1":1}}]}

    cli.print_verbose_diagnostics(diagnostics); output=capsys.readouterr().out

    assert "Potential: REJECTED" in output
    assert "Potential Rejection Reason: MISSING_POTENTIAL_FIELDS: target_2, rr_t2, entry_rr" in output


def test_symbol_analysis_prints_calibrated_fundamental_diagnostics(capsys):
    cli.print_symbol_analysis({"symbol":"TERA.IS","fundamental":{"fundamental_score":100,
        "effective_score":43.61,"coverage":38.46,"confidence":49.42}})
    output=capsys.readouterr().out
    assert '"raw_score": 100' in output
    assert '"effective_score": 43.61' in output
    assert '"coverage": 38.46' in output
    assert '"confidence": 49.42' in output


def test_verbose_formatter_prints_bank_profile_metrics(capsys):
    diagnostics={"candidates":[{"symbol":"BANK.IS","decision":"HOLD","final_score":70,
        "reason":"fixture","news_status":"NO_NEWS","llm_status":"NOT_REQUIRED",
        "signal_mode":"STAGED_INVESTMENT","fundamental":{"fundamental_score":72,
            "effective_score":61,"coverage":85.71,"confidence":76,
            "score_breakdown":{"profile":"BANK","metric_values":{"net_income_yoy":9,
                "net_interest_income_yoy":11,"loan_growth":14,"deposit_growth":None,
                "roe":.16,"roa":.02,"equity_assets":.12}}}}]}

    cli.print_verbose_diagnostics(diagnostics); output=capsys.readouterr().out

    assert "Profile: BANK" in output and "Net Interest Income YoY: 11" in output
    assert "Loan Growth: 14" in output and "Deposit Growth: UNKNOWN" in output
    assert "ROE: 0.16" in output and "ROA: 0.02" in output and "Equity / Assets: 0.12" in output


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
