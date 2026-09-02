"""SQLite 存储 + 字段级变更历史。

为什么要存历史：入学要求每年都改。第一次全量人工过一遍之后，
以后每次重抓只需要看 diff——这是长期维护唯一可行的方式。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Program

SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
    program_key       TEXT PRIMARY KEY,
    university        TEXT NOT NULL,
    code              TEXT NOT NULL,
    title             TEXT NOT NULL,
    level             TEXT NOT NULL,
    cricos_code       TEXT,
    credit_points     INTEGER,
    duration_full_time TEXT,
    faculty           TEXT,
    campus            TEXT,
    intakes           TEXT,
    ielts_overall     REAL,
    ielts_min_band    REAL,
    min_wam_percent   REAL,
    requires_cognate  INTEGER,
    source_url        TEXT NOT NULL,
    source_updated_at TEXT,
    fetched_at        TEXT NOT NULL,
    payload           TEXT NOT NULL,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    program_key TEXT NOT NULL,
    changed_at  TEXT NOT NULL,
    field       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    program_key TEXT NOT NULL,
    url         TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    path        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_programs_uni   ON programs(university);
CREATE INDEX IF NOT EXISTS idx_programs_level ON programs(level);
CREATE INDEX IF NOT EXISTS idx_changes_key    ON changes(program_key);
"""

# 值得追踪变更的字段——录取要求相关的，改了必须有人看见
TRACKED_FIELDS = [
    "title", "level", "credit_points", "duration_full_time", "faculty",
    "ielts_overall", "ielts_min_band", "min_wam_percent", "requires_cognate",
    "source_updated_at",
]


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.conn.commit()
        self.close()

    @staticmethod
    def _row_from(program: Program) -> dict[str, Any]:
        return {
            "program_key": program.program_key,
            "university": program.university,
            "code": program.code,
            "title": program.title,
            "level": program.level,
            "cricos_code": program.cricos_code,
            "credit_points": program.credit_points,
            "duration_full_time": program.duration_full_time,
            "faculty": program.faculty,
            "campus": json.dumps(program.campus, ensure_ascii=False),
            "intakes": json.dumps(program.intakes, ensure_ascii=False),
            "ielts_overall": program.english.ielts_overall,
            "ielts_min_band": program.english.ielts_min_band,
            "min_wam_percent": program.entry.min_wam_percent,
            "requires_cognate": (
                None if program.entry.requires_cognate_degree is None
                else int(program.entry.requires_cognate_degree)
            ),
            "source_url": program.source_url,
            "source_updated_at": program.source_updated_at,
            "fetched_at": program.fetched_at.isoformat(),
            "payload": program.model_dump_json(),
        }

    def upsert(self, program: Program) -> list[tuple[str, Any, Any]]:
        """写入并返回本次发生变化的字段 [(field, old, new), ...]。"""
        row = self._row_from(program)
        now = datetime.now(timezone.utc).isoformat()
        existing = self.conn.execute(
            "SELECT * FROM programs WHERE program_key = ?", (program.program_key,)
        ).fetchone()

        diffs: list[tuple[str, Any, Any]] = []
        if existing is not None:
            for field in TRACKED_FIELDS:
                old, new = existing[field], row[field]
                if old != new:
                    diffs.append((field, old, new))
                    self.conn.execute(
                        "INSERT INTO changes (program_key, changed_at, field, old_value, new_value)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (program.program_key, now, field, str(old), str(new)),
                    )

        row["first_seen"] = existing["first_seen"] if existing else now
        row["last_seen"] = now
        columns = ", ".join(row)
        placeholders = ", ".join(f":{c}" for c in row)
        self.conn.execute(
            f"INSERT OR REPLACE INTO programs ({columns}) VALUES ({placeholders})", row
        )
        self.conn.commit()
        return diffs

    def record_snapshot(self, program_key: str, url: str, fetched_at: datetime,
                        sha256: str, path: Path) -> None:
        self.conn.execute(
            "INSERT INTO snapshots (program_key, url, fetched_at, sha256, path)"
            " VALUES (?, ?, ?, ?, ?)",
            (program_key, url, fetched_at.isoformat(), sha256, str(path)),
        )
        self.conn.commit()

    # ------------------------------------------------------------------
    # 查询——这些以后会直接变成 agent 的 tool
    # ------------------------------------------------------------------
    def search(
        self,
        keyword: str | None = None,
        university: str | None = None,
        level: str | None = None,
        max_ielts: float | None = None,
        max_wam: float | None = None,
        limit: int = 50,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM programs WHERE 1=1"
        params: list[Any] = []
        if keyword:
            sql += " AND (title LIKE ? OR faculty LIKE ? OR code LIKE ?)"
            params += [f"%{keyword}%"] * 3
        if university:
            sql += " AND university LIKE ?"
            params.append(f"%{university}%")
        if level:
            sql += " AND level = ?"
            params.append(level)
        if max_ielts is not None:
            sql += " AND ielts_overall IS NOT NULL AND ielts_overall <= ?"
            params.append(max_ielts)
        if max_wam is not None:
            sql += " AND min_wam_percent IS NOT NULL AND min_wam_percent <= ?"
            params.append(max_wam)
        sql += " ORDER BY university, code LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def get(self, program_key: str) -> Program | None:
        row = self.conn.execute(
            "SELECT payload FROM programs WHERE program_key = ?", (program_key,)
        ).fetchone()
        return Program.model_validate_json(row["payload"]) if row else None

    def recent_changes(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM changes ORDER BY changed_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()

    def all_programs(self) -> list[Program]:
        rows = self.conn.execute(
            "SELECT payload FROM programs ORDER BY university, code"
        ).fetchall()
        return [Program.model_validate_json(r["payload"]) for r in rows]

    def stats(self) -> dict[str, Any]:
        cur = self.conn.execute(
            "SELECT university, level, COUNT(*) n,"
            " SUM(ielts_overall IS NOT NULL) with_ielts,"
            " SUM(min_wam_percent IS NOT NULL) with_wam"
            " FROM programs GROUP BY university, level ORDER BY university, level"
        )
        return {"by_group": [dict(r) for r in cur.fetchall()],
                "total": self.conn.execute("SELECT COUNT(*) c FROM programs").fetchone()["c"]}
