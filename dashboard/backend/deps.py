"""FastAPI dependencies."""
from __future__ import annotations

import sqlite3
from typing import Iterator

from . import userdb as userdb_mod


def get_user_conn() -> Iterator[sqlite3.Connection]:
    conn = userdb_mod.open_rw()
    try:
        yield conn
    finally:
        conn.close()
