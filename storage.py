# -*- coding: utf-8 -*-
"""SQLite 数据层：缓存指数估值历史，支持增量入库。

表结构：
  valuation(symbol, indicator, date, value, PK(symbol,indicator,date))
  fetch_log(symbol, indicator, last_fetch, PK(symbol,indicator))
"""

import sqlite3


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    # WAL 提升并发、减少写锁；树莓派单进程其实够用，开着无妨
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    conn = get_conn(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS valuation(
            symbol    TEXT,
            indicator TEXT,
            date      TEXT,
            value     REAL,
            PRIMARY KEY(symbol, indicator, date));
        CREATE INDEX IF NOT EXISTS idx_val_sym_ind
            ON valuation(symbol, indicator, date);
        CREATE TABLE IF NOT EXISTS fetch_log(
            symbol     TEXT,
            indicator  TEXT,
            last_fetch TEXT,
            PRIMARY KEY(symbol, indicator));
        """
    )
    conn.commit()
    return conn


def latest_db_date(conn, symbol: str, indicator: str):
    """DB 里该序列最新日期 'YYYY-MM-DD'，无数据返回 None。"""
    row = conn.execute(
        "SELECT MAX(date) FROM valuation WHERE symbol=? AND indicator=?",
        (symbol, indicator),
    ).fetchone()
    return row[0] if row and row[0] else None


def upsert_series(conn, symbol, indicator, series):
    """增量写入。series: list[(date, value)]。
    返回 (new, changed)：新行数、值发生变化的行数。
    """
    new, changed = 0, 0
    for date, value in series:
        old = conn.execute(
            "SELECT value FROM valuation "
            "WHERE symbol=? AND indicator=? AND date=?",
            (symbol, indicator, date),
        ).fetchone()
        if old is None:
            new += 1
        elif old[0] != value:
            changed += 1
        conn.execute(
            "INSERT OR REPLACE INTO valuation(symbol,indicator,date,value) "
            "VALUES(?,?,?,?)",
            (symbol, indicator, date, value),
        )
    conn.commit()
    return new, changed


def set_fetch_date(conn, symbol, indicator, date_str: str):
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log(symbol,indicator,last_fetch) "
        "VALUES(?,?,?)",
        (symbol, indicator, date_str),
    )
    conn.commit()


def load_series(conn, symbol, indicator):
    """按日期升序读出 list[(date, value)]。"""
    cur = conn.execute(
        "SELECT date, value FROM valuation "
        "WHERE symbol=? AND indicator=? ORDER BY date",
        (symbol, indicator),
    )
    return [(r[0], r[1]) for r in cur.fetchall()]