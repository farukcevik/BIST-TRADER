from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from .schema import SCHEMA


class Database:
    def __init__(self, path: str, *, read_only: bool=False):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro",uri=True) if read_only else sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        if not read_only:
            self.connection.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        columns={row["name"] for row in self.connection.execute("PRAGMA table_info(paper_positions)")}
        additions={"current_score":"REAL","data_timestamp":"TEXT",
                   "position_status":"TEXT NOT NULL DEFAULT 'UNKNOWN'",
                   "initial_quantity":"INTEGER","target_1":"TEXT","target_2":"TEXT",
                   "target_3":"TEXT","stop_price":"TEXT",
                   "target_1_hit":"INTEGER NOT NULL DEFAULT 0",
                   "target_2_hit":"INTEGER NOT NULL DEFAULT 0"}
        for name,declaration in additions.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE paper_positions ADD COLUMN {name} {declaration}")
        for table in ("paper_orders","paper_fills"):
            tier_columns={row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")}
            if "buy_tier" not in tier_columns:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN buy_tier TEXT")
        kap_columns={row["name"] for row in self.connection.execute("PRAGMA table_info(kap_member_cache)")}
        if "outstanding_shares" not in kap_columns:
            self.connection.execute("ALTER TABLE kap_member_cache ADD COLUMN outstanding_shares REAL")
        if "company_type" not in kap_columns:
            self.connection.execute("ALTER TABLE kap_member_cache ADD COLUMN company_type TEXT")
        poll_columns={row["name"] for row in self.connection.execute("PRAGMA table_info(kap_disclosure_poll_state)")}
        if "lease_owner" not in poll_columns:
            self.connection.execute("ALTER TABLE kap_disclosure_poll_state ADD COLUMN lease_owner TEXT")
        if "lease_expires_at" not in poll_columns:
            self.connection.execute("ALTER TABLE kap_disclosure_poll_state ADD COLUMN lease_expires_at TEXT")
        self.connection.commit()

    def execute(self, sql: str, parameters: Iterable[Any] = ()) -> sqlite3.Cursor:
        cursor = self.connection.execute(sql, tuple(parameters)); self.connection.commit(); return cursor

    def query(self, sql: str, parameters: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, tuple(parameters)))

    def close(self) -> None: self.connection.close()
    def __enter__(self) -> "Database": return self
    def __exit__(self, *args: object) -> None: self.close()
