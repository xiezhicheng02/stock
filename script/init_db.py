#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首次建库：建表 + 写入默认配置 + 初始化估值目标。**只执行一次**。

用法：
    python3 script/init_db.py                 # 首次建库（已初始化过则直接退出）
    python3 script/init_db.py --force         # 已初始化也重跑（把配置/目标恢复为默认值）
    python3 script/init_db.py --db /path/stock.db
    STOCK_DB=/tmp/t.db python3 script/init_db.py     # 用环境变量指定库

什么时候需要它
--------------
只在**第一次**把系统跑起来之前执行一次，用来创建空库并写入出厂默认值。
之后结构与配置的增量升级全部由 Web 服务启动时自动完成：

    storage.ensure_schema()   → 建表 / 补列 / 清理冗余索引（schema.migrate）
    config.ensure_settings()  → 补进新增配置键 / 清掉已废弃的键

所以日常（含版本升级）**不需要**再跑 init_db；本脚本检测到库已初始化就会
拒绝执行，避免把手工调过的配置和估值目标覆盖回默认值。

说明：
    * 默认配置的**唯一来源**是 src/config/defaults.py；
    * 配置写入数据库后，程序通过 src/config/config.py 的纯函数式接口读取；
    * 估值目标（指数/个股）与五指标权重写入 valuation_target 表。
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from src.config.defaults import (DEFAULT_SETTINGS, TARGET_SPECS,  # noqa: E402
                                 dump_value, smtp_source_note)
from src.storage import schema  # noqa: E402


def initialized_version(conn) -> int:
    """已初始化过的库版本号；0 = 这个库还没初始化过（可以建库）。

    判据是 schema_version 表里有没有版本记录——无论这次建库是 init_db 还是
    Web 服务启动时的 ensure_schema 完成的，只要写进去了就算"已初始化"。
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='schema_version'").fetchone():
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    return int(row[0]) if row and row[0] else 0


def init_schema(conn):
    conn.executescript(schema.DDL)
    conn.commit()


def migrate_columns(conn):
    """结构升级：补列 + 清理冗余对象（幂等，逻辑在 schema.py 里统一维护）。"""
    return schema.migrate(conn)


def record_version(conn):
    """记录表结构版本（库比代码新时直接报错）。"""
    try:
        cur, now = schema.stamp_version(conn)
    except RuntimeError as e:
        raise SystemExit(str(e)) from e
    if cur < now:
        print(f"结构版本：v{cur} → v{now}")
    else:
        print(f"结构版本：v{cur}（已是最新）")


def import_settings(conn, force=False):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    added = skipped = 0
    for key, value, val_type, group, remark in DEFAULT_SETTINGS:
        if conn.execute("SELECT 1 FROM setting WHERE key=?", (key,)).fetchone() and not force:
            skipped += 1
            continue
        conn.execute(
            "INSERT OR REPLACE INTO setting"
            "(key,value,val_type,group_name,remark,updated_at) VALUES(?,?,?,?,?,?)",
            (key, dump_value(value, val_type), val_type, group, remark, now),
        )
        added += 1
    conn.commit()
    return added, skipped


def import_targets(conn, force=False):
    added = skipped = 0
    for (code, name, ktype, enabled, w_pe, w_pb, w_ps, w_pcf,
         w_div, sort, remark) in TARGET_SPECS:
        if conn.execute("SELECT 1 FROM valuation_target WHERE code=?", (code,)).fetchone() \
                and not force:
            skipped += 1
            continue
        conn.execute(
            """INSERT OR REPLACE INTO valuation_target
               (code,name,ktype,enabled,w_pe,w_pb,w_ps,w_pcf,w_dividend,sort_order,remark)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (code, name, ktype, enabled, w_pe, w_pb, w_ps, w_pcf, w_div, sort, remark),
        )
        added += 1
    conn.commit()
    return added, skipped


def main():
    ap = argparse.ArgumentParser(description="首次建库（只执行一次）")
    ap.add_argument("--db", default=os.environ.get("STOCK_DB", "data/stock.db"),
                    help="数据库路径（默认 data/stock.db，可用 STOCK_DB 环境变量）")
    ap.add_argument("--force", action="store_true",
                    help="库已初始化时仍重跑：配置与估值目标会被覆盖回默认值")
    args = ap.parse_args()

    db_path = os.path.abspath(args.db)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    print(f"数据库：{db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    # ---- 只跑一次：库一旦初始化过就不再执行（除非 --force）----
    ver = initialized_version(conn)
    if ver and not args.force:
        conn.close()
        print(f"\n跳过：该数据库已初始化过（结构版本 v{ver}）。")
        print("init_db 只在系统首次运行时执行一次；日常升级由服务启动时自动完成：")
        print("  · 表结构/索引 → storage.ensure_schema()")
        print("  · 配置增删   → config.ensure_settings()")
        print("如确需把配置和估值目标恢复为出厂默认值，请显式加 --force。")
        return 1

    print(smtp_source_note())

    init_schema(conn)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(f"建表完成：{len(tables)} 张 -> {', '.join(tables)}")

    cols = migrate_columns(conn)
    if cols:
        print("结构升级：" + ", ".join(cols))

    record_version(conn)

    # 漂移检查：有人改了 DDL 却忘了写迁移，这里立刻提示（而不是等写入时报错）
    diffs = schema.drift(conn)
    if diffs:
        print("⚠ 表结构与代码定义不一致（请把新列补进 schema.MIGRATION_COLUMNS）：")
        for d in diffs:
            print("   -", d)
    a1, s1 = import_settings(conn, args.force)
    print(f"配置导入：新增/更新 {a1} 项，跳过 {s1} 项")
    a2, s2 = import_targets(conn, args.force)
    print(f"估值目标：新增/更新 {a2} 项，跳过 {s2} 项")

    print("\n--- 配置分组统计 ---")
    for g, n in conn.execute(
            "SELECT group_name, COUNT(*) FROM setting GROUP BY group_name ORDER BY group_name"):
        print(f"  {g:10s} {n} 项")

    print("--- 估值目标 ---")
    for row in conn.execute(
            "SELECT code,name,enabled,w_pe,w_pb,w_ps,w_pcf,w_dividend "
            "FROM valuation_target ORDER BY sort_order"):
        code, name, en, pe, pb, ps, pcf, dv = row
        flag = "启用" if en else "禁用"
        print(f"  [{flag}] {name:8s} {code}  PE{pe:.0%} PB{pb:.0%} PS{ps:.0%} "
              f"PCF{pcf:.0%} 股息{dv:.0%}  合计{pe+pb+ps+pcf+dv:.0%}")

    conn.close()
    print("\n完成。之后请直接启动服务（./script/run_web.sh），无需再次执行本脚本。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
