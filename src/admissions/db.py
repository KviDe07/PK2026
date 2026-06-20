"""Хранилище на SQLite: схема и слой доступа к данным.

Excel — только формат выгрузки отчётов. Источник правды и история статусов
живут здесь, в одном файле-БД (по умолчанию data/db/applicants.sqlite).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .models import CANONICAL_FIELDS
from .utils import now_iso

# ── DDL ──────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS applicants (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    last_name      TEXT,
    first_name     TEXT,
    middle_name    TEXT,
    birth_date     TEXT,
    email          TEXT,
    phone          TEXT,
    snils          TEXT,
    passport       TEXT,
    education_doc  TEXT,
    program        TEXT,
    education_form TEXT,
    funding_basis  TEXT,
    total_score    REAL,
    consent        INTEGER,
    bitrix_id      TEXT,
    onec_id        TEXT,
    current_status TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_app_snils    ON applicants(snils);
CREATE INDEX IF NOT EXISTS idx_app_email    ON applicants(email);
CREATE INDEX IF NOT EXISTS idx_app_phone    ON applicants(phone);
CREATE INDEX IF NOT EXISTS idx_app_bitrix   ON applicants(bitrix_id);
CREATE INDEX IF NOT EXISTS idx_app_onec     ON applicants(onec_id);

CREATE TABLE IF NOT EXISTS source_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    applicant_id  INTEGER REFERENCES applicants(id) ON DELETE SET NULL,
    source        TEXT NOT NULL,
    source_key    TEXT NOT NULL,
    status_raw    TEXT,
    status        TEXT,
    payload       TEXT,          -- JSON: канонические поля
    raw           TEXT,          -- JSON: исходная строка
    ingest_run_id INTEGER REFERENCES ingest_runs(id) ON DELETE SET NULL,
    ingested_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_src_lookup ON source_records(source, source_key);

CREATE TABLE IF NOT EXISTS status_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    applicant_id    INTEGER NOT NULL REFERENCES applicants(id) ON DELETE CASCADE,
    status          TEXT NOT NULL,
    previous_status TEXT,
    source          TEXT,
    changed_at      TEXT NOT NULL,
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_hist_app ON status_history(applicant_id);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source         TEXT NOT NULL,
    file_name      TEXT,
    file_hash      TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    records_total  INTEGER DEFAULT 0,
    records_new    INTEGER DEFAULT 0,
    records_updated INTEGER DEFAULT 0,
    status_changes INTEGER DEFAULT 0,
    note           TEXT
);

CREATE TABLE IF NOT EXISTS review_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,        -- 'match' (неоднозначное слияние) | 'conflict' (расхождение полей)
    source        TEXT,
    source_key    TEXT,
    applicant_id  INTEGER,
    record_name   TEXT,
    reason        TEXT,
    candidate_ids TEXT,
    score         REAL,
    ingest_run_id INTEGER,
    created_at    TEXT NOT NULL,
    resolved      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_review_open ON review_queue(resolved);
"""

# Колонки applicants, которые можно заполнять из канонических полей.
_APPLICANT_COLUMNS = list(CANONICAL_FIELDS) + ["bitrix_id", "onec_id", "current_status"]


class Database:
    """Тонкий слой доступа к SQLite."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── ingest_runs ───────────────────────────────────────────────────────────
    def hash_already_ingested(self, source: str, file_hash: str) -> bool:
        """Был ли уже успешно загружен файл с таким хешем (идемпотентность)."""
        row = self.conn.execute(
            "SELECT 1 FROM ingest_runs WHERE source=? AND file_hash=? AND finished_at IS NOT NULL",
            (source, file_hash),
        ).fetchone()
        return row is not None

    def create_ingest_run(
        self, source: str, file_name: Optional[str], file_hash: Optional[str], note: str = ""
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO ingest_runs (source, file_name, file_hash, started_at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (source, file_name, file_hash, now_iso(), note),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_ingest_run(self, run_id: int, **counts: int) -> None:
        fields = {"finished_at": now_iso(), **counts}
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE ingest_runs SET {sets} WHERE id=?",
            (*fields.values(), run_id),
        )
        self.conn.commit()

    # ── applicants ────────────────────────────────────────────────────────────
    def insert_applicant(self, data: Dict[str, Any]) -> int:
        cols = [c for c in _APPLICANT_COLUMNS if c in data]
        ts = now_iso()
        all_cols = cols + ["created_at", "updated_at"]
        placeholders = ", ".join("?" for _ in all_cols)
        values = [_coerce(c, data.get(c)) for c in cols] + [ts, ts]
        cur = self.conn.execute(
            f"INSERT INTO applicants ({', '.join(all_cols)}) VALUES ({placeholders})",
            values,
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_applicant(self, applicant_id: int, data: Dict[str, Any]) -> None:
        cols = [c for c in _APPLICANT_COLUMNS if c in data]
        if not cols:
            return
        sets = ", ".join(f"{c}=?" for c in cols) + ", updated_at=?"
        values = [_coerce(c, data.get(c)) for c in cols] + [now_iso(), applicant_id]
        self.conn.execute(f"UPDATE applicants SET {sets} WHERE id=?", values)
        self.conn.commit()

    def get_applicant(self, applicant_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM applicants WHERE id=?", (applicant_id,)
        ).fetchone()

    def all_applicants(self) -> List[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM applicants ORDER BY id").fetchall()

    def find_by_field(self, field: str, value: Any) -> List[sqlite3.Row]:
        """Поиск кандидатов по точному значению индексируемого поля."""
        if field not in _APPLICANT_COLUMNS or value in (None, ""):
            return []
        return self.conn.execute(
            f"SELECT * FROM applicants WHERE {field}=?", (value,)
        ).fetchall()

    # ── source_records ──────────────────────────────────────────────────────────
    def add_source_record(
        self,
        applicant_id: Optional[int],
        source: str,
        source_key: str,
        status_raw: Optional[str],
        status: Optional[str],
        payload: Dict[str, Any],
        raw: Dict[str, Any],
        ingest_run_id: Optional[int],
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO source_records "
            "(applicant_id, source, source_key, status_raw, status, payload, raw, ingest_run_id, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                applicant_id, source, source_key, status_raw, status,
                json.dumps(payload, ensure_ascii=False, default=str),
                json.dumps(raw, ensure_ascii=False, default=str),
                ingest_run_id, now_iso(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # ── status_history ──────────────────────────────────────────────────────────
    def add_status_history(
        self,
        applicant_id: int,
        status: str,
        previous_status: Optional[str],
        source: Optional[str],
        note: str = "",
    ) -> None:
        self.conn.execute(
            "INSERT INTO status_history "
            "(applicant_id, status, previous_status, source, changed_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (applicant_id, status, previous_status, source, now_iso(), note),
        )
        self.conn.commit()

    def status_changes_since(self, since_iso: str) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT h.*, a.last_name, a.first_name, a.middle_name "
            "FROM status_history h JOIN applicants a ON a.id = h.applicant_id "
            "WHERE h.changed_at >= ? ORDER BY h.changed_at",
            (since_iso,),
        ).fetchall()

    # ── review_queue ─────────────────────────────────────────────────────────────
    def add_review_item(
        self,
        kind: str,
        source: str,
        source_key: str,
        record_name: str,
        reason: str,
        candidate_ids: Optional[List[int]] = None,
        score: Optional[float] = None,
        applicant_id: Optional[int] = None,
        ingest_run_id: Optional[int] = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO review_queue "
            "(kind, source, source_key, applicant_id, record_name, reason, candidate_ids, score, ingest_run_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind, source, source_key, applicant_id, record_name, reason,
                ",".join(str(i) for i in (candidate_ids or [])),
                score, ingest_run_id, now_iso(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_review_items(self, unresolved_only: bool = True) -> List[sqlite3.Row]:
        sql = "SELECT * FROM review_queue"
        if unresolved_only:
            sql += " WHERE resolved = 0"
        sql += " ORDER BY created_at DESC"
        return self.conn.execute(sql).fetchall()

    # ── произвольные запросы для отчётов ─────────────────────────────────────────
    def query(self, sql: str, params: Iterable[Any] = ()) -> List[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()


def _coerce(column: str, value: Any) -> Any:
    """Привести значение к типу колонки SQLite."""
    if value is None:
        return None
    if column == "consent":
        return int(bool(value))
    if column == "total_score":
        try:
            return float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            return None
    return value
