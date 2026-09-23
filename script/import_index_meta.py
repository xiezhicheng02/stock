#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性导入：baostock 文档里的**指数元数据**（560+ 只）→ stock_basic。

为什么需要它
------------
baostock 行情接口（query_stock_basic）只给"代码 + 简称 + 上市日"，拿不到指数
**全称 / 类别 / 发布机构 / 简介**。这些只在文档页 dataExplain.md 的「指数数据」
章节里，共 10 张表：综合、规模、一级行业、二级行业、策略、成长、价值、主题、
基金、债券指数。

抓的什么、写到哪
----------------
    POST https://www.baostock.com/helpdocs/api/markdown/dataExplain.md
        ↓ 解析（按 `### <a id=...>` 标题归属类别，容错见 data_fetcher.parse_index_doc）
    stock_basic  ← name(简称) / full_name / category / publisher / intro /
                   listed_date(发布日期) / market / meta_src='baostock-doc'

**只写元数据**：不碰 kline / valuation_score / valuation_target，
所以不会影响任何评分、分位、邮件。

用法
----
    python3 script/import_index_meta.py --dry-run          # 只抓+解析+打印，不写库
    python3 script/import_index_meta.py                    # 写默认库
    python3 script/import_index_meta.py --db /tmp/t.db     # 写指定库
    python3 script/import_index_meta.py --html /tmp/doc.html   # 用本地已存的正文（离线重跑）
    STOCK_DB=/tmp/t.db python3 script/import_index_meta.py

重复执行是**幂等**的：已有 index 行原地更新，没有的才新增。

⚠️ ktype 不是 'index' 的代码一律跳过（只打印）：自定义组合可以用任意代码，
实测 `sh.000922` 既是文档里的「中证红利」指数，也是用户自建的组合，
盲目写会把 portfolio 改成 index。
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from src.config import config                    # noqa: E402
from src.fetch_data import data_fetcher as fetcher  # noqa: E402
from src.storage import storage                  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="导入 baostock 文档里的指数元数据")
    ap.add_argument("--db", default=None, help="数据库路径（默认 STOCK_DB / data/stock.db）")
    ap.add_argument("--html", default=None,
                    help="改用本地文件作为文档正文（不联网，便于离线核对）")
    ap.add_argument("--url", default=fetcher.INDEX_DOC_URL, help="文档接口地址")
    ap.add_argument("--dry-run", action="store_true", help="只抓取+解析+打印，不写库")
    args = ap.parse_args()

    if args.html:
        with open(args.html, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        print("文档来源：本地文件 %s（%d 字节）" % (args.html, len(text)))
    else:
        print("文档来源：%s" % args.url)
        text = fetcher.fetch_index_doc(args.url)
        print("已下载：%d 字节" % len(text))

    rows, meta = fetcher.parse_index_doc(text)
    print("\n解析结果：%d 只指数（%d 张表）" % (len(rows), meta["tables"]))
    for cat, n in meta["categories"].items():
        print("    %-14s %4d" % (cat, n))
    if meta["duplicates"]:
        print("  文档内重复代码（已去重留首次）：%s" % ", ".join(meta["duplicates"]))
    if meta["bad_date"]:
        print("  发布日期解析失败：%s" % ", ".join(meta["bad_date"]))
    no_intro = [r["code"] for r in rows if not r["intro"]]
    if no_intro:
        print("  原文简介为空：%d 只（保留为空，不编造）" % len(no_intro))

    if args.dry_run:
        print("\n--dry-run：不写库。样例：")
        for r in rows[:3]:
            print("    %s %s | %s | %s | %s"
                  % (r["code"], r["name"], r["full_name"],
                     r["publish_date"], r["category"]))
        return 0

    # config.use_db 必须在 storage/config 真正读库之前调用（它要求库已初始化）
    config.use_db(args.db)
    conn = storage.get_conn()
    try:
        storage.ensure_schema(conn)          # 补 v10 新增的 5 个列
        res = fetcher.sync_index_meta(conn, text)   # 抓取层唯一的写入路径
    finally:
        conn.close()

    print("\n写入完成：新增 %d / 更新 %d / 跳过 %d"
          % (res["inserted"], res["updated"], len(res["skipped"])))
    if res["skipped"]:
        print("  跳过（代码已被非 index 行占用）：")
        for s in res["skipped"]:
            print("    %s  当前 ktype=%s" % (s["code"], s["ktype"]))
    stats = res["stats"]
    print("\nstock_basic 指数现状：共 %d 只，其中有简介 %d 只"
          % (stats["total"], stats["with_intro"]))
    for cat, n in stats["by_category"].items():
        print("    %-14s %4d" % (cat, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
