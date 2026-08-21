SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS symbols(symbol TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS market_snapshots(id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, symbol TEXT NOT NULL, price REAL NOT NULL, volume REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS technical_signals(id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, symbol TEXT NOT NULL, score REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS news_items(id INTEGER PRIMARY KEY, source_id TEXT, source TEXT, url TEXT, timestamp TEXT, title TEXT, body TEXT, symbol TEXT, relevance REAL, content_hash TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS kap_items(id INTEGER PRIMARY KEY, source_id TEXT, source TEXT, url TEXT, timestamp TEXT, title TEXT, body TEXT, symbol TEXT, relevance REAL, content_hash TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS event_items(id TEXT PRIMARY KEY, symbol TEXT NOT NULL, source TEXT NOT NULL, source_type TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, url TEXT, published_at TEXT NOT NULL, fetched_at TEXT NOT NULL, hash TEXT UNIQUE NOT NULL, trust_score REAL NOT NULL);
CREATE TABLE IF NOT EXISTS intelligence_rankings(id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL, created_at TEXT NOT NULL, rank INTEGER NOT NULL, symbol TEXT NOT NULL, scanner_score REAL NOT NULL, event_score REAL NOT NULL, combined_score REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_analyses(id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, symbol TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_analysis_cache(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, input_hash TEXT NOT NULL, market_state_hash TEXT NOT NULL, event_hashes TEXT NOT NULL, symbol TEXT NOT NULL, model_name TEXT NOT NULL, prompt_version TEXT NOT NULL, output TEXT NOT NULL, latency_ms REAL NOT NULL, input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, status TEXT NOT NULL, UNIQUE(input_hash,model_name,prompt_version));
CREATE TABLE IF NOT EXISTS trade_signals(id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL, score REAL NOT NULL, reason TEXT NOT NULL, strategy_version TEXT NOT NULL, requested_price REAL NOT NULL);
CREATE TABLE IF NOT EXISTS risk_decisions(id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, signal_id TEXT NOT NULL, approved INTEGER NOT NULL, reason TEXT NOT NULL, quantity INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_decision_records(id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, signal_id TEXT NOT NULL, outcome TEXT NOT NULL, reason_code TEXT NOT NULL, reason TEXT NOT NULL, requested_quantity INTEGER, approved_quantity INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, signal_id TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL, quantity INTEGER NOT NULL, requested_price REAL NOT NULL, fill_price REAL, score REAL NOT NULL, reason TEXT NOT NULL, strategy_version TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, order_id TEXT NOT NULL, symbol TEXT NOT NULL, action TEXT NOT NULL, quantity INTEGER NOT NULL, fill_price REAL NOT NULL, commission REAL NOT NULL, realized_pnl REAL NOT NULL);
CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY, quantity INTEGER NOT NULL, average_price REAL NOT NULL, high_price REAL NOT NULL, opened_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_orders(id TEXT PRIMARY KEY,signal_id TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,quantity INTEGER NOT NULL,requested_price TEXT NOT NULL,fill_price TEXT,timestamp TEXT NOT NULL,reason TEXT NOT NULL,final_score REAL NOT NULL,risk_decision TEXT NOT NULL,strategy_version TEXT NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_fills(id TEXT PRIMARY KEY,order_id TEXT NOT NULL,timestamp TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL,quantity INTEGER NOT NULL,fill_price TEXT NOT NULL,commission TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_positions(symbol TEXT PRIMARY KEY,quantity INTEGER NOT NULL,average_price TEXT NOT NULL,last_price TEXT NOT NULL,high_price TEXT NOT NULL,opened_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS portfolio_snapshots(id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, cash REAL NOT NULL, portfolio_value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS system_events(id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, level TEXT NOT NULL, event_type TEXT NOT NULL, message TEXT NOT NULL, payload TEXT NOT NULL);
"""
