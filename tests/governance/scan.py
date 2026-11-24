from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class Match:
    location: str
    excerpt: str

    def __str__(self) -> str:
        return f"{self.location}: {self.excerpt}"


def find_in_directory(root: Path, needle: str) -> list[Match]:
    if not root.exists():
        return []
    matches: list[Match] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        index = text.find(needle)
        if index == -1:
            continue
        start = max(0, index - 40)
        end = min(len(text), index + len(needle) + 40)
        matches.append(Match(location=str(path), excerpt=text[start:end]))
    return matches


def find_in_sqlite(db_path: Path, needle: str) -> list[Match]:
    if not db_path.exists():
        return []
    matches: list[Match] = []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        for table in tables:
            cursor.execute(f'PRAGMA table_info("{table}")')
            columns = [row[1] for row in cursor.fetchall()]
            if not columns:
                continue
            quoted_columns = ", ".join(f'"{c}"' for c in columns)
            try:
                cursor.execute(f'SELECT rowid, {quoted_columns} FROM "{table}"')
                has_rowid = True
            except sqlite3.OperationalError:
                cursor.execute(f'SELECT {quoted_columns} FROM "{table}"')
                has_rowid = False
            for row in cursor.fetchall():
                rowid, values = (row[0], row[1:]) if has_rowid else (None, row)
                for column, value in zip(columns, values):
                    if value is None:
                        continue
                    text = value if isinstance(value, str) else str(value)
                    if needle in text:
                        index = text.find(needle)
                        start = max(0, index - 40)
                        end = min(len(text), index + len(needle) + 40)
                        matches.append(
                            Match(
                                location=f"{db_path}::{table}.{column} (rowid={rowid})",
                                excerpt=text[start:end],
                            )
                        )
    finally:
        conn.close()
    return matches
