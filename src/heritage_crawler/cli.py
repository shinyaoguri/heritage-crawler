"""コマンドライン。初回の全件取得はここからローカルで回す (ADR 0006)。

2 段構え (ADR 0002) をそのままコマンドにしてある。

1. ``fetch-ledger`` で分類 × 地域の CSV を取る (153 リクエスト / 約 25 分)
2. ``fetch-detail`` で台帳の各行から詳細ページを取る (2 万件 / 約 6 時間)

どちらも中断しても取得済みを飛ばして再開する。既定の間隔は 1 秒・並列度 1 で、
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

from heritage_crawler.cache import DEFAULT_CACHE_DIR, DetailCache, LedgerCache, detail_key
from heritage_crawler.catalog import BUILDING_CATEGORIES, SEARCH_AREAS, Area, Category
from heritage_crawler.detail import (
    DetailError,
    Target,
    fetch_details,
    format_detail_summary,
    read_targets,
    summarize_details,
)
from heritage_crawler.export import DEFAULT_OUTPUT_DIR, build_dataset, format_report
from heritage_crawler.http import (
    DEFAULT_CONTACT,
    DEFAULT_INTERVAL,
    DEFAULT_TIMEOUT,
    FetchError,
    PoliteClient,
    RateLimiter,
)
from heritage_crawler.ledger import LedgerError, fetch_ledgers, format_summary, summarize
from heritage_crawler.search_page import ParseError

CONTACT_ENV: Final = "HERITAGE_CRAWLER_CONTACT"
MAX_CONCURRENCY: Final = 8
"""並列度の上限。

レート自体は共有の RateLimiter が抑えるので、これ以上増やしても速くならず、
相手側の同時接続だけが増える。
"""

logger = logging.getLogger("heritage_crawler")


def _target_options(parser: argparse.ArgumentParser) -> None:
    """どの分類・地域を相手にするか。詳細ページの対象も台帳経由でこれで絞る。"""
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        choices=[category.code for category in BUILDING_CATEGORIES],
        help="対象の分類コード (既定: 101 102 103)",
    )
    parser.add_argument(
        "--area",
        action="append",
        dest="areas",
        choices=[area.name for area in SEARCH_AREAS],
        metavar="地域名",
        help="対象の地域名 (既定: 47 都道府県 + ２県以上 + 地域を定めない)",
    )


def _access_options(parser: argparse.ArgumentParser) -> None:
    """相手先へのアクセスの仕方 (ADR 0002 のマナー)。"""
    parser.add_argument(
        "--interval",
        type=_seconds,
        default=DEFAULT_INTERVAL,
        help=f"リクエスト間隔の秒数 (既定: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--timeout", type=_seconds, default=DEFAULT_TIMEOUT, help="1 リクエストのタイムアウト秒数"
    )
    parser.add_argument(
        "--contact",
        default=os.environ.get(CONTACT_ENV, DEFAULT_CONTACT),
        help=f"User-Agent に載せる連絡先 (環境変数 {CONTACT_ENV} でも指定できる)",
    )
    parser.add_argument("--force", action="store_true", help="取得済みのぶんも取り直す")


def _seconds(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("負の秒数は指定できない")
    return number


def _concurrency(value: str) -> int:
    number = int(value)
    if not 1 <= number <= MAX_CONCURRENCY:
        raise argparse.ArgumentTypeError(f"並列度は 1〜{MAX_CONCURRENCY} にする")
    return number


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
    _target_options(fetch)
    _access_options(fetch)

    subparsers.add_parser("report-ledger", help="キャッシュ済みの台帳を既知の件数と突き合わせる")

    detail = subparsers.add_parser("fetch-detail", help="台帳の各行から詳細ページを取得する")
    _target_options(detail)
    _access_options(detail)
    detail.add_argument(
        "--concurrency",
        type=_concurrency,
        default=1,
        help=f"同時に投げる本数 (既定: 1 = 逐次、最大 {MAX_CONCURRENCY})。"
        "レートは並列でも 1 本ぶんに保たれる",
    )
    detail.add_argument(
        "--limit", type=int, help="先頭から指定件数だけ取る (疎通確認や様子見に使う)"
    )
    detail.add_argument(
        "--retry-failed", action="store_true", help="失敗として記録されたぶんだけ取り直す"
    )

    report_detail = subparsers.add_parser("report-detail", help="詳細ページの取得状況を報告する")
    _target_options(report_detail)

    build = subparsers.add_parser(
        "build-records", help="キャッシュから都道府県ごとの JSON Lines を組み立てる"
    )
    _target_options(build)
    build.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="データリポジトリを並べた親ディレクトリ (既定: "
        f"{DEFAULT_OUTPUT_DIR})。配下に <リポジトリ名>/data/ を作る (ADR 0009)",
    )
    return parser


def _selected[Item: (Category, Area)](
    wanted: Sequence[str] | None, everything: Sequence[Item], key: str
) -> list[Item]:
    """指定が無ければ全部。指定があっても、順序は定義順のまま保つ。"""
    if not wanted:
        return list(everything)
    chosen = set(wanted)
    return [item for item in everything if getattr(item, key) in chosen]


RESUME_HINT: Final = "取得済みは記録済みなので、原因を直せば同じコマンドで再開できる"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    ledger_cache = LedgerCache(args.cache_dir)
    categories = _selected(getattr(args, "categories", None), BUILDING_CATEGORIES, "code")
    areas = _selected(getattr(args, "areas", None), SEARCH_AREAS, "name")

    if args.command.endswith("-ledger"):
        return _run_ledger(args, ledger_cache, categories, areas)
    if args.command == "build-records":
        return _run_build(args, ledger_cache, categories, areas)
    return _run_detail(args, ledger_cache, categories, areas)


def _run_ledger(
    args: argparse.Namespace,
    cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area],
) -> int:
    if args.command == "fetch-ledger":
        client = PoliteClient(contact=args.contact, interval=args.interval, timeout=args.timeout)
        try:
            fetch_ledgers(client, cache, categories, areas, force=args.force)
        except KeyboardInterrupt:
            logger.warning("中断した。取得済みは記録済みなので、同じコマンドで再開できる")
            return 130
        except (FetchError, LedgerError, ParseError) as error:
            logger.error("%s", error)
            logger.error("%s", RESUME_HINT)
            return 1

    print(format_summary(summarize(cache, categories, areas)))
    return 0


def _run_detail(
    args: argparse.Namespace,
    ledger_cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area],
) -> int:
    cache = DetailCache(args.cache_dir)
    targets = read_targets(ledger_cache, categories, areas)

    if args.command == "fetch-detail":
        status = _fetch_detail(args, cache, targets)
        if status:
            return status

    print(format_detail_summary(summarize_details(cache, targets), cache.failures()))
    return 0


def _run_build(
    args: argparse.Namespace,
    ledger_cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area],
) -> int:
    """外部へは出ない。キャッシュだけを読んで JSON Lines を書く (ADR 0004)。"""
    report = build_dataset(
        ledger_cache, DetailCache(args.cache_dir), categories, areas, args.output_dir
    )
    print(format_report(report))
    if report.total == 0:
        logger.error("台帳が空。先に heritage-crawler fetch-ledger を実行する")
        return 1
    # 異常があっても書き出したものは残す。捨てずに報告して、直すかどうかは人が決める。
    return 0


def _fetch_detail(args: argparse.Namespace, cache: DetailCache, targets: Sequence[Target]) -> int:
    if not targets:
        logger.error("台帳が空。先に heritage-crawler fetch-ledger を実行する")
        return 1

    wanted = list(targets)
    if args.retry_failed:
        failed = {
            detail_key(entry.daichou_id, entry.kanri_taishou_id) for entry in cache.failures()
        }
        wanted = [target for target in wanted if target.key in failed]
        if not wanted:
            logger.info("失敗として記録されたものは無い")
            return 0
    if args.limit is not None:
        wanted = wanted[: args.limit]

    # レートを 1 本の共有ゲートで抑え、クライアントは並列度ぶん作る (ADR 0002)。
    limiter = RateLimiter(args.interval)
    fetchers = [
        PoliteClient(contact=args.contact, timeout=args.timeout, limiter=limiter)
        for _ in range(args.concurrency)
    ]
    try:
        fetch_details(fetchers, cache, wanted, force=args.force)
    except KeyboardInterrupt:
        logger.warning("中断した。取得済みは記録済みなので、同じコマンドで再開できる")
        return 130
    except (FetchError, DetailError, LedgerError) as error:
        logger.error("%s", error)
        logger.error("%s", RESUME_HINT)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
