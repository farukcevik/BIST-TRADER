from __future__ import annotations

import sys

import pytest

from bistbot import main as cli
from bistbot.broker.paper import PaperBroker
from bistbot.portfolio.controls import PaperPortfolioControlService
from bistbot.storage.database import Database
from bistbot.storage.paper_reset import (CASH_VERSION_KEY, DATABASE_ENVIRONMENT_KEY,
                                         PAPER_RESET_TABLES, reset_paper_state)


def _seed_all_reset_tables(database: Database) -> None:
    connection=database.connection
    connection.execute("PRAGMA foreign_keys=OFF")
    for table in PAPER_RESET_TABLES:
        columns=connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        values=[]
        for column in columns:
            if column["name"]=="buy_tier": values.append("NORMAL")
            elif "INT" in column["type"].upper(): values.append(1)
            elif "REAL" in column["type"].upper(): values.append(1.0)
            else: values.append(f"seed-{column['name']}")
        placeholders=",".join("?" for _ in values)
        connection.execute(f'INSERT INTO "{table}" VALUES({placeholders})',values)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("INSERT INTO symbols(symbol,active) VALUES('THYAO.IS',1)")
    connection.execute("INSERT INTO runtime_settings VALUES('kept.setting','7','now',1)")
    connection.execute("INSERT INTO metadata VALUES('kept.metadata','yes')")
    connection.execute("INSERT INTO metadata VALUES('paper_cash','100.00')")
    connection.execute("INSERT INTO metadata VALUES('paper_initial_capital','100.00')")
    connection.commit()


def test_full_paper_reset_is_atomic_and_preserves_schema_and_configuration(tmp_path):
    database=Database(str(tmp_path/"paper.db"))
    try:
        _seed_all_reset_tables(database)
        capital=reset_paper_state(database,mode="PAPER",capital=200000)

        assert capital.as_tuple().exponent==-2
        assert all(database.query(f'SELECT COUNT(*) AS count FROM "{table}"')[0]["count"]==0
                   for table in PAPER_RESET_TABLES)
        metadata={row["key"]:row["value"] for row in database.query("SELECT key,value FROM metadata")}
        assert metadata["paper_cash"]=="200000.00"
        assert metadata["paper_initial_capital"]=="200000.00"
        assert metadata["paper_realized_pnl"]=="0.00"
        assert metadata[DATABASE_ENVIRONMENT_KEY]=="PAPER"
        assert metadata["kept.metadata"]=="yes"
        assert database.query("SELECT symbol FROM symbols")[0]["symbol"]=="THYAO.IS"
        assert database.query("SELECT value FROM runtime_settings WHERE key='kept.setting'")[0]["value"]=="7"
        assert database.query("SELECT name FROM sqlite_master WHERE type='table' AND name='paper_orders'")
    finally:
        database.close()


@pytest.mark.parametrize("database_mode",["LIVE","SHADOW"])
def test_reset_refuses_mismatched_database_identity_without_mutation(tmp_path,database_mode):
    database=Database(str(tmp_path/"paper.db"))
    try:
        database.execute("INSERT INTO metadata(key,value) VALUES(?,?)",
                         (DATABASE_ENVIRONMENT_KEY,database_mode))
        database.execute("INSERT INTO technical_signals(timestamp,symbol,score,payload) "
                         "VALUES('now','AAA',1,'{}')")
        with pytest.raises(PermissionError,match=f"database is marked {database_mode}"):
            reset_paper_state(database,mode="PAPER",capital=200000)
        assert database.query("SELECT COUNT(*) AS count FROM technical_signals")[0]["count"]==1
        assert database.query("SELECT value FROM metadata WHERE key=?",
                              (DATABASE_ENVIRONMENT_KEY,))[0]["value"]==database_mode
        assert not database.query("SELECT value FROM metadata WHERE key='paper_cash'")
    finally:
        database.close()


def test_reset_advances_cash_version_and_rejects_stale_control_request(tmp_path):
    database=Database(str(tmp_path/"paper.db"))
    try:
        database.execute("INSERT INTO metadata(key,value) VALUES('paper_cash','150000.00')")
        database.execute("INSERT INTO runtime_settings(key,value,updated_at,version) VALUES(?,?,?,?)",
                         (CASH_VERSION_KEY,"7","before",3))
        database.execute("INSERT INTO runtime_settings(key,value,updated_at,version) VALUES(?,?,?,?)",
                         ("paper.max_open_positions","4","before",2))

        reset_paper_state(database,mode="PAPER",capital=200000)

        controls=PaperPortfolioControlService(database,mode="PAPER",configured_max_open_positions=5)
        state=controls.state()
        assert state.cash_version==8
        assert state.max_open_positions_override==4
        with pytest.raises(RuntimeError,match="STALE_CONTROL_VERSION"):
            controls.set_cash(123000,expected_version=7,idempotency_token="pre-reset-request")
        assert controls.state().cash==200000
    finally:
        database.close()


def test_normal_paper_broker_initialization_marks_database_paper(tmp_path):
    database=Database(str(tmp_path/"paper.db"))
    try:
        PaperBroker(200000,database=database)
        assert database.query("SELECT value FROM metadata WHERE key=?",
                              (DATABASE_ENVIRONMENT_KEY,))[0]["value"]=="PAPER"
    finally:
        database.close()


def test_paper_broker_adopts_unmarked_analysis_and_audit_state(tmp_path):
    database=Database(str(tmp_path/"analysis.db"))
    try:
        database.execute("INSERT INTO technical_signals(timestamp,symbol,score,payload) VALUES('now','AAA',1,'{}')")
        database.execute("INSERT INTO llm_analyses(timestamp,symbol,payload) VALUES('now','AAA','{}')")
        database.execute("INSERT INTO risk_decision_records(timestamp,signal_id,outcome,reason_code,reason,"
                         "requested_quantity,approved_quantity,payload) "
                         "VALUES('now','signal','REJECT','INVALID_REQUEST','audit',1,0,'{}')")
        PaperBroker(200000,database=database)
        assert database.query("SELECT value FROM metadata WHERE key=?",
                              (DATABASE_ENVIRONMENT_KEY,))[0]["value"]=="PAPER"
        assert database.query("SELECT COUNT(*) n FROM technical_signals")[0]["n"]==1
        assert database.query("SELECT COUNT(*) n FROM llm_analyses")[0]["n"]==1
        assert database.query("SELECT COUNT(*) n FROM risk_decision_records")[0]["n"]==1
    finally:
        database.close()


@pytest.mark.parametrize(("table","insert_sql"),[
    ("paper_positions","INSERT INTO paper_positions(symbol,quantity,average_price,last_price,high_price,opened_at,updated_at) VALUES('AAA',1,'10','10','10','now','now')"),
    ("paper_portfolio_snapshots","INSERT INTO paper_portfolio_snapshots(timestamp,cash,equity,realized_pnl,unrealized_pnl) VALUES('now','1','1','0','0')"),
])
def test_paper_broker_refuses_unmarked_paper_execution_state(tmp_path,table,insert_sql):
    database=Database(str(tmp_path/f"unsafe-{table}.db"))
    try:
        database.execute(insert_sql)
        with pytest.raises(PermissionError,match="unmarked database contains runtime state"):
            PaperBroker(200000,database=database)
        assert database.query(f'SELECT COUNT(*) n FROM "{table}"')[0]["n"]==1
        assert not database.query("SELECT key FROM metadata")
    finally:
        database.close()


@pytest.mark.parametrize(("table", "insert_sql"),[
    ("orders", "INSERT INTO orders(id,timestamp,signal_id,symbol,action,quantity,requested_price,fill_price,"
               "score,reason,strategy_version,status) VALUES('order','now','signal','AAA','BUY',1,10,NULL,"
               "50,'generic','v1','PENDING')"),
    ("trades", "INSERT INTO trades(timestamp,order_id,symbol,action,quantity,fill_price,commission,realized_pnl) "
               "VALUES('now','order','AAA','BUY',1,10,0,0)"),
    ("positions", "INSERT INTO positions(symbol,quantity,average_price,high_price,opened_at,updated_at) "
                  "VALUES('AAA',1,10,10,'now','now')"),
])
def test_paper_broker_refuses_unmarked_generic_state_without_mutation(tmp_path,table,insert_sql):
    database=Database(str(tmp_path/f"unknown-{table}.db"))
    try:
        database.execute(insert_sql)
        before=[tuple(row) for row in database.query(f'SELECT * FROM "{table}"')]
        with pytest.raises(PermissionError,match="unmarked database contains runtime state"):
            PaperBroker(200000,database=database)
        assert [tuple(row) for row in database.query(f'SELECT * FROM "{table}"')]==before
        assert not database.query("SELECT key FROM metadata")
    finally:
        database.close()


def test_paper_broker_refuses_conflicting_database_marker_without_mutation(tmp_path):
    database=Database(str(tmp_path/"live.db"))
    try:
        database.execute("INSERT INTO metadata(key,value) VALUES(?, 'LIVE')",(DATABASE_ENVIRONMENT_KEY,))
        with pytest.raises(PermissionError,match="database is marked LIVE"):
            PaperBroker(200000,database=database)
        assert [(row["key"],row["value"]) for row in database.query("SELECT key,value FROM metadata")]==[
            (DATABASE_ENVIRONMENT_KEY,"LIVE")]
    finally:
        database.close()


def test_paper_broker_adopts_legacy_paper_accounting_database(tmp_path):
    database=Database(str(tmp_path/"legacy-broker.db"))
    try:
        database.execute("INSERT INTO metadata(key,value) VALUES('paper_cash','123.00')")
        database.execute("INSERT INTO metadata(key,value) VALUES('paper_initial_capital','1000.00')")
        database.execute("INSERT INTO paper_positions(symbol,quantity,average_price,last_price,high_price,"
                         "opened_at,updated_at) VALUES('AAA',1,'10','10','10','now','now')")
        broker=PaperBroker(200000,database=database)
        assert broker.get_cash()==123
        assert database.query("SELECT value FROM metadata WHERE key=?",
                              (DATABASE_ENVIRONMENT_KEY,))[0]["value"]=="PAPER"
        assert database.query("SELECT symbol FROM paper_positions")[0]["symbol"]=="AAA"
    finally:
        database.close()


def test_truly_empty_unmarked_database_can_be_initialized_by_reset(tmp_path):
    database=Database(str(tmp_path/"fresh.db"))
    try:
        reset_paper_state(database,mode="PAPER",capital=200000)
        metadata={row["key"]:row["value"] for row in database.query("SELECT key,value FROM metadata")}
        assert metadata[DATABASE_ENVIRONMENT_KEY]=="PAPER"
        assert metadata["paper_cash"]=="200000.00"
    finally:
        database.close()


def test_unmarked_generic_trading_database_is_refused_without_mutation(tmp_path):
    database=Database(str(tmp_path/"unknown.db"))
    try:
        database.execute("INSERT INTO orders(id,timestamp,signal_id,symbol,action,quantity,requested_price,"
                         "fill_price,score,reason,strategy_version,status) "
                         "VALUES('order','now','signal','AAA','BUY',1,10,NULL,50,'generic','v1','PENDING')")
        with pytest.raises(PermissionError,match="unmarked database contains runtime state"):
            reset_paper_state(database,mode="PAPER",capital=200000)
        assert database.query("SELECT id FROM orders")[0]["id"]=="order"
        assert not database.query("SELECT value FROM metadata WHERE key=?",(DATABASE_ENVIRONMENT_KEY,))
        assert not database.query("SELECT value FROM metadata WHERE key='paper_cash'")
    finally:
        database.close()


def test_unmarked_legacy_paper_accounting_database_is_accepted(tmp_path):
    database=Database(str(tmp_path/"legacy-paper.db"))
    try:
        database.execute("INSERT INTO metadata(key,value) VALUES('paper_cash','123.00')")
        database.execute("INSERT INTO metadata(key,value) VALUES('paper_initial_capital','1000.00')")
        database.execute("INSERT INTO paper_positions(symbol,quantity,average_price,last_price,high_price,"
                         "opened_at,updated_at) VALUES('AAA',1,'10','10','10','now','now')")
        reset_paper_state(database,mode="PAPER",capital=200000)
        assert not database.query("SELECT symbol FROM paper_positions")
        assert database.query("SELECT value FROM metadata WHERE key=?",
                              (DATABASE_ENVIRONMENT_KEY,))[0]["value"]=="PAPER"
        assert database.query("SELECT value FROM metadata WHERE key='paper_cash'")[0]["value"]=="200000.00"
    finally:
        database.close()


def test_reset_rolls_back_every_change_if_any_delete_fails(tmp_path):
    database=Database(str(tmp_path/"paper.db"))
    try:
        database.execute("INSERT INTO market_snapshots(timestamp,symbol,price,volume,payload) "
                         "VALUES('now','AAA',1,1,'{}')")
        database.execute("INSERT INTO technical_signals(timestamp,symbol,score,payload) "
                         "VALUES('now','AAA',1,'{}')")
        database.execute("INSERT INTO metadata(key,value) VALUES('paper_cash','100.00')")
        database.execute("INSERT INTO metadata(key,value) VALUES('paper_initial_capital','100.00')")
        database.execute("CREATE TRIGGER stop_reset BEFORE DELETE ON technical_signals "
                         "BEGIN SELECT RAISE(ABORT,'test reset failure'); END")
        with pytest.raises(Exception,match="test reset failure"):
            reset_paper_state(database,mode="PAPER",capital=200000)
        assert database.query("SELECT COUNT(*) AS count FROM market_snapshots")[0]["count"]==1
        assert database.query("SELECT COUNT(*) AS count FROM technical_signals")[0]["count"]==1
    finally:
        database.close()


@pytest.mark.parametrize("mode",["LIVE","SHADOW","",None])
def test_reset_refuses_every_non_paper_mode_without_mutation(tmp_path,mode):
    database=Database(str(tmp_path/"paper.db"))
    try:
        database.execute("INSERT INTO technical_signals(timestamp,symbol,score,payload) VALUES('now','AAA',1,'{}')")
        with pytest.raises(PermissionError,match="PAPER reset refused"):
            reset_paper_state(database,mode=mode,capital=200000)
        assert database.query("SELECT COUNT(*) AS count FROM technical_signals")[0]["count"]==1
    finally:
        database.close()


def test_cli_requires_confirmation_before_opening_database(tmp_path,monkeypatch,capsys):
    config=tmp_path/"config.yaml"
    config.write_text("system:\n  mode: paper\ndatabase: should-not-exist.db\n",encoding="utf-8")
    monkeypatch.setattr(sys,"argv",["main.py","--config",str(config),"--reset-paper"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code==2
    assert "requires explicit --confirm" in capsys.readouterr().err
    assert not (tmp_path/"should-not-exist.db").exists()


@pytest.mark.parametrize("mode",["live","shadow"])
def test_cli_refuses_live_and_shadow_before_settings_or_database_load(tmp_path,monkeypatch,capsys,mode):
    config=tmp_path/"config.yaml"
    config.write_text(f"system:\n  mode: {mode}\ndatabase: should-not-exist.db\n",encoding="utf-8")
    monkeypatch.setattr(sys,"argv",["main.py","--config",str(config),"--reset-paper","--confirm"])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code==2
    assert f"refused in {mode.upper()} mode" in capsys.readouterr().err
    assert not (tmp_path/"should-not-exist.db").exists()
