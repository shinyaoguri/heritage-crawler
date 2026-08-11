"""コマンドライン。初回の全件取得はここからローカルで回す (ADR 0006)。

``fetch-ledger`` は中断しても取得済みを飛ばして再開する。既定の間隔は 1 秒で、
相手が公共サイトである以上むやみに縮めない (ADR 0002)。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from heritage_crawler.cache import DEFAULT_CACHE_DIR, LedgerCache
from heritage_crawler.catalog import BUILDING_CATEGORIES, SEARCH_AREAS, Area, Category
from heritage_crawler.http import (
    DEFAULT_CONTACT,
    DEFAULT_INTERVAL,
    DEFAULT_TIMEOUT,
    FetchError,
    PoliteClient,
)
from heritage_crawler.ledger import LedgerError, fetch_ledgers, format_summary, summarize
from heritage_crawler.search_page import ParseError

CONTACT_ENV: Final = "HERITAGE_CRAWLER_CONTACT"

logger = logging.getLogger("heritage_crawler")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="heritage-crawler",
        description="国指定文化財等データベースから建造物関連データを取得する",
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="キャッシュの置き場"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="デバッグログも出す")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch-ledger", help="分類 × 地域で CSV の台帳を取得する")
    fetch.add_argument(
        "--category",
        action="append",
        dest="categories",
        choices=[category.code for category in BUILDING_CATEGORIES],
        help="対象の分類コード (既定: 101 102 103)",
    )
    fetch.add_argument(
        "--area",
        action="append",
        dest="areas",
        choices=[area.name for area in SEARCH_AREAS],
        metavar="地域名",
        help="対象の地域名 (既定: 47 都道府県 + ２県以上 + 地域を定めない)",
    )
    fetch.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"リクエスト間隔の秒数 (既定: {DEFAULT_INTERVAL})",
    )
    fetch.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="1 リクエストのタイムアウト秒数"
    )
    fetch.add_argument(
        "--contact",
        default=os.environ.get(CONTACT_ENV, DEFAULT_CONTACT),
        help=f"User-Agent に載せる連絡先 (環境変数 {CONTACT_ENV} でも指定できる)",
    )
    fetch.add_argument("--force", action="store_true", help="取得済みのぶんも取り直す")

    subparsers.add_parser("report-ledger", help="キャッシュ済みの台帳を既知の件数と突き合わせる")
    return parser


def _selected[Item: (Category, Area)](
    wanted: Sequence[str] | None, everything: Sequence[Item], key: str
) -> list[Item]:
    """指定が無ければ全部。指定があっても、順序は定義順のまま保つ。"""
    if not wanted:
        return list(everything)
    chosen = set(wanted)
    return [item for item in everything if getattr(item, key) in chosen]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    cache = LedgerCache(args.cache_dir)
    categories = _selected(getattr(args, "categories", None), BUILDING_CATEGORIES, "code")
    areas = _selected(getattr(args, "areas", None), SEARCH_AREAS, "name")

    if args.command == "fetch-ledger":
        client = PoliteClient(contact=args.contact, interval=args.interval, timeout=args.timeout)
        try:
            fetch_ledgers(client, cache, categories, areas, force=args.force)
        except KeyboardInterrupt:
            logger.warning("中断した。取得済みは記録済みなので、同じコマンドで再開できる")
            return 130
        except (FetchError, LedgerError, ParseError) as error:
            logger.error("%s", error)
            logger.error("取得済みは記録済みなので、原因を直せば同じコマンドで再開できる")
            return 1

    print(format_summary(summarize(cache, categories, areas)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
