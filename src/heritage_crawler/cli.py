"""コマンドライン。初回の全件取得はここからローカルで回す (ADR 0006)。

2 段構え (ADR 0002) をそのままコマンドにしてある。

1. ``fetch-ledger`` で分類 × 地域の CSV を取る (204 リクエスト / 約 30 分)
2. ``fetch-detail`` で台帳の各行から詳細ページを取る (約 2.4 万件 / 約 6.6 時間)

どちらも中断しても取得済みを飛ばして再開する。既定のレート上限は 1 req/s
(間隔 1 秒)。相手はこれを超えると 200 でエラーページを返す (ADR 0011)。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from heritage_crawler.cache import DEFAULT_CACHE_DIR, DetailCache, LedgerCache, detail_key
from heritage_crawler.catalog import (
    TARGET_CATEGORIES,
    Area,
    Category,
    all_search_areas,
    datasets_for,
)
from heritage_crawler.detail import (
    DetailError,
    Target,
    fetch_details,
    format_detail_summary,
    probe_presence,
    read_targets,
    recheck_cache,
    summarize_details,
)
from heritage_crawler.export import DEFAULT_OUTPUT_DIR, Checked, build_dataset, format_report
from heritage_crawler.http import (
    DEFAULT_CONTACT,
    DEFAULT_INTERVAL,
    DEFAULT_TIMEOUT,
    FetchError,
    PoliteClient,
    RateLimiter,
)
from heritage_crawler.ledger import (
    LedgerError,
    LedgerRun,
    Session,
    fetch_ledgers,
    format_summary,
    read_ledger_rows,
    summarize,
)
from heritage_crawler.listing import (
    ListingError,
    audit_listing,
    fetch_listing,
    format_audits,
    recover_missing,
)
from heritage_crawler.metadata import JST
from heritage_crawler.readme import ReadmeError, read_counts, render_block, replace_block
from heritage_crawler.search_page import ParseError
from heritage_crawler.status import checked_today
from heritage_crawler.update import (
    ROTATION_SLOTS,
    LedgerDiff,
    UpdateError,
    candidates,
    compare_ledgers,
    format_ledger_diff,
    format_plan,
    format_verification,
    plan_update,
    read_existing,
    reuse_for,
    verify_removals,
)

ALL_AREAS: Final = all_search_areas(TARGET_CATEGORIES)
"""``--area`` で選べる地域。分類ごとの分割軸をまとめたもの (重複は落とす)。

**既定では使わない。** 指定が無ければ地域は分類ごとに決まる (``catalog.areas_for``)。
"""

CONTACT_ENV: Final = "HERITAGE_CRAWLER_CONTACT"
MAX_CONCURRENCY: Final = 8
"""並列度の上限。

レートの上限は間隔が決めるので (ADR 0010)、応答待ちの隙間が埋まったあとは
これ以上増やしても速くならず、相手側の同時接続だけが増える。
既定の間隔 1 秒では 1 本で足りる。
"""

DEFAULT_README: Final = Path("README.md")

DEFAULT_CONCURRENCY: Final = 1
"""詳細ページ取得の既定の並列度。

間隔 1 秒・応答 0.625 秒なら 1 本で上限を出しきるので、増やす理由が無い
(増えるのは相手側の同時接続だけ。ADR 0011)。
"""

logger = logging.getLogger("heritage_crawler")


def _target_options(parser: argparse.ArgumentParser) -> None:
    """どの分類・地域を相手にするか。詳細ページの対象も台帳経由でこれで絞る。

    **既定の説明は語彙から組み立てる。** 直書きした値は語彙が増えた月に嘘になり、
    401 を加えたとき (#27) に「既定: 101 102 103」が実際に取り残された (#38)。
    """
    parser.add_argument(
        "--category",
        action="append",
        dest="categories",
        choices=[category.code for category in TARGET_CATEGORIES],
        help=f"対象の分類コード (既定: {' '.join(c.code for c in TARGET_CATEGORIES)})",
    )
    parser.add_argument(
        "--area",
        action="append",
        dest="areas",
        choices=[area.name for area in ALL_AREAS if area.name],
        # 60 個を並べると usage が読めなくなる。分類と違って選択肢は伏せる
        metavar="地域名",
        help=(
            "対象の地域名 (指定しなければ分類ごとの分割軸。"
            f"選べるのは全 {len(ALL_AREAS)} 地域)"
        ),
    )


def _access_options(parser: argparse.ArgumentParser, *, resumable: bool = True) -> None:
    """相手先へのアクセスの仕方 (ADR 0002 のマナー)。

    ``resumable`` が偽なら ``--force`` を出さない。取得済みを飛ばす仕組みが
    無いコマンド (毎回すべて取り直すもの) では意味を持たないため。
    """
    parser.add_argument(
        "--interval",
        type=_seconds,
        default=DEFAULT_INTERVAL,
        help=f"リクエスト間隔の秒数 = レート上限 (既定: {DEFAULT_INTERVAL} = 1 req/s)",
    )
    parser.add_argument(
        "--timeout", type=_seconds, default=DEFAULT_TIMEOUT, help="1 リクエストのタイムアウト秒数"
    )
    parser.add_argument(
        "--contact",
        default=os.environ.get(CONTACT_ENV, DEFAULT_CONTACT),
        help=f"User-Agent に載せる連絡先 (環境変数 {CONTACT_ENV} でも指定できる)",
    )
    if resumable:
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


def _concurrency_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--concurrency",
        type=_concurrency,
        default=DEFAULT_CONCURRENCY,
        help=f"同時に投げる本数 (既定: {DEFAULT_CONCURRENCY}、最大 {MAX_CONCURRENCY})。"
        "レートの上限は間隔が決めるので、増やしても超えない",
    )


def _output_dir_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="データリポジトリを並べた親ディレクトリ (既定: "
        f"{DEFAULT_OUTPUT_DIR})。配下に <リポジトリ名>/data/ を作る (ADR 0009)",
    )


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

    audit = subparsers.add_parser(
        "audit-listing", help="検索結果一覧を全ページ辿って台帳の網羅性を確かめる"
    )
    _target_options(audit)
    _access_options(audit, resumable=False)
    audit.add_argument(
        "--recover",
        action="store_true",
        help="地域では引けない指定を一覧から台帳へ回収する (ADR 0017)",
    )

    compare = subparsers.add_parser(
        "compare-ledgers",
        help="前回の台帳と今回をバイト単位で突き合わせる (ADR 0020)",
        description="相手先へは一切アクセスしない。手元の CSV どうしを比べるだけ。",
    )
    _target_options(compare)
    compare.add_argument(
        "--previous", type=Path, required=True, help="前回の台帳ディレクトリ (ledger/)"
    )
    compare.add_argument(
        "--json", type=Path, help="結果を JSON で書き出す先 (ワークフローが分岐に使う)"
    )

    detail = subparsers.add_parser("fetch-detail", help="台帳の各行から詳細ページを取得する")
    _target_options(detail)
    _access_options(detail)
    _concurrency_option(detail)
    detail.add_argument(
        "--limit", type=int, help="先頭から指定件数だけ取る (疎通確認や様子見に使う)"
    )
    detail.add_argument(
        "--retry-failed", action="store_true", help="失敗として記録されたぶんだけ取り直す"
    )
    detail.add_argument(
        "--recheck-cache",
        action="store_true",
        help="取得の前にキャッシュ済みの HTML を検査し、エラーページだったものを取り直す "
        "(200 で返るエラーページを掴んでいた場合の復旧。ADR 0011)",
    )

    report_detail = subparsers.add_parser("report-detail", help="詳細ページの取得状況を報告する")
    _target_options(report_detail)

    build = subparsers.add_parser(
        "build-records", help="キャッシュから都道府県ごとの JSON Lines を組み立てる"
    )
    _target_options(build)
    _output_dir_option(build)

    update = subparsers.add_parser(
        "update-records",
        help="前回の出力と台帳を突き合わせ、差分だけ取り直して書き直す (ADR 0018 / ADR 0020)",
        description="週次の差分更新。先に fetch-ledger で台帳を取り直しておく。",
    )
    _target_options(update)
    _access_options(update, resumable=False)
    _concurrency_option(update)
    _output_dir_option(update)
    update.add_argument(
        "--slot",
        type=_slot,
        help=f"巡回の枠 1〜{ROTATION_SLOTS} (既定: 実行週の ISO 週番号)。"
        f"全体の 1/{ROTATION_SLOTS} を毎週取り直す",
    )
    update.add_argument(
        "--previous-ledger",
        type=Path,
        help="前回の台帳ディレクトリ。渡すと CSV 同士を突き合わせて変更を見つける",
    )
    update.add_argument(
        "--dry-run",
        action="store_true",
        help="計画だけを出す (相手先へは一切アクセスせず、出力も書き換えない)",
    )
    update.add_argument(
        "--checked-date",
        type=_date,
        # **既定は None。** 空文字にすると argparse が既定値にも type を通し、
        # 何も渡していないのに「日付として読めない」で落ちる。
        default=None,
        help="確認日 (YYYY-MM-DD。既定: 日本時間の今日)。データが変わらなくても "
        "各データリポジトリの status.json に残す (ADR 0023)",
    )
    update.add_argument(
        "--run-url",
        default="",
        help="この実行への URL。status.json に残して、あとから追えるようにする",
    )

    render = subparsers.add_parser(
        "render-readme",
        help="README の件数表を書き出したデータから作り直す",
        description="散文に写した件数は新規指定・解除のたびに嘘になるので生成する (Issue #37)。",
    )
    _output_dir_option(render)
    render.add_argument(
        "--readme", type=Path, default=DEFAULT_README, help=f"書き換える先 (既定: {DEFAULT_README})"
    )
    render.add_argument(
        "--check",
        action="store_true",
        help="書き換えず、ずれていれば異常終了する (あるべき表を出力する)",
    )
    return parser


def _date(value: str) -> str:
    """``YYYY-MM-DD`` として読めることだけ確かめる。

    確認日は残り続ける記録なので、読めない値を書いてから気付くと直しにくい
    (次の週に上書きされるまで、データリポジトリ 10 個に残る)。
    """
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"日付は YYYY-MM-DD で書く: {value!r}") from error


def _slot(value: str) -> int:
    number = int(value)
    if not 1 <= number <= ROTATION_SLOTS:
        raise argparse.ArgumentTypeError(f"巡回の枠は 1〜{ROTATION_SLOTS} にする")
    return number


def _slot_of(now: datetime) -> int:
    """実行日の巡回の枠 (1〜52)。

    **日本時間の ISO 週で決める。** 年をまたいでも連続し、週の切れ目が月曜に揃う
    (週次の実行と枠が 1 対 1 で対応する)。53 週ある年は最後の週が 1 番と重なり、
    その年だけ 1 番の枠が 2 回当たる — 取り直しが 1 回増えるだけなので許容する。
    """
    return (now.astimezone(JST).isocalendar().week - 1) % ROTATION_SLOTS + 1


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
    categories = _selected(getattr(args, "categories", None), TARGET_CATEGORIES, "code")
    # 指定が無ければ None。**地域は分類ごとに違う**ので、下流が分類から決める
    # (catalog.areas_for)。_selected は指定が無いと全部を返すので、ここでは通さない。
    wanted_areas = getattr(args, "areas", None)
    areas = _selected(wanted_areas, ALL_AREAS, "name") if wanted_areas else None

    if args.command == "compare-ledgers":
        return _run_compare(args, ledger_cache, categories)
    if args.command.endswith("-ledger"):
        return _run_ledger(args, ledger_cache, categories, areas)
    if args.command == "audit-listing":
        return _run_audit(args, ledger_cache, categories, areas)
    if args.command == "build-records":
        return _run_build(args, ledger_cache, categories, areas)
    if args.command == "update-records":
        return _run_update(args, ledger_cache, categories, areas)
    if args.command == "render-readme":
        return _run_render_readme(args)
    return _run_detail(args, ledger_cache, categories, areas)


def _run_compare(
    args: argparse.Namespace, cache: LedgerCache, categories: Sequence[Category]
) -> int:
    """前回の台帳と今回を突き合わせて報告する。

    **相手先へは出ない。** 手元の CSV どうしを比べるだけなので、週次の実行が
    「台帳を取り直す必要があるか」を決めるのにも使える。
    """
    diff = compare_ledgers(args.previous, cache.ledger_dir, categories)
    print(format_ledger_diff(diff))
    if args.json:
        payload = {
            "changed": bool(diff.changed_files),
            "compared_files": diff.compared_files,
            "changed_files": list(diff.changed_files),
            "changed_records": {key: list(columns) for key, columns in diff.changed.items()},
        }
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0


def _run_ledger(
    args: argparse.Namespace,
    cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area] | None,
) -> int:
    run: LedgerRun | None = None
    if args.command == "fetch-ledger":
        client = PoliteClient(contact=args.contact, interval=args.interval, timeout=args.timeout)
        try:
            run = fetch_ledgers(client, cache, categories, areas, force=args.force)
        except KeyboardInterrupt:
            logger.warning("中断した。取得済みは記録済みなので、同じコマンドで再開できる")
            return 130

    # 取れたぶんの報告は、失敗があっても出す。どこまで進んだかが次の回の入力になる。
    print(format_summary(summarize(cache, categories, areas)))
    if run is None or run.ok:
        return 0

    if run.abort_reason:
        logger.error("%s", run.abort_reason)
    logger.error("%d 件を取れなかった: %s", len(run.failures), _listed(run.failures))
    logger.error("%s", RESUME_HINT)
    return 1


def _listed(items: Sequence[str], limit: int = 10) -> str:
    """失敗の一覧。204 地域ぶん並べても読めないので頭だけ出す。"""
    head = "、".join(items[:limit])
    return head if len(items) <= limit else f"{head} ほか {len(items) - limit} 件"


def _run_audit(
    args: argparse.Namespace,
    cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area] | None,
) -> int:
    """一覧を全ページ辿って台帳と突き合わせる (ADR 0017 / ADR 0026)。

    一覧のキーが台帳のキーに載らない分類は飛ばす。1 件でも取りこぼしが
    残っていれば異常終了する — 気付かずに次の工程へ進まないため。
    """
    client = PoliteClient(contact=args.contact, interval=args.interval, timeout=args.timeout)
    session = Session(client)
    audits = []
    recovered = 0
    try:
        for category in categories:
            if not category.audits_with_listing:
                logger.info("%s の一覧のキーは台帳のキーに載らないので飛ばす", category.code)
                continue
            audit = audit_listing(cache, fetch_listing(client, session, category), areas)
            audits.append(audit)
            if args.recover:
                recovered += recover_missing(cache, audit)
    except KeyboardInterrupt:
        logger.warning("中断した")
        return 130
    except (FetchError, ListingError, LedgerError, ParseError) as error:
        logger.error("%s", error)
        return 1

    if not audits:
        logger.error("突き合わせられる分類が無い (102 以外の分類を --category で選ぶ)")
        return 1

    print(format_audits(audits))
    missing = sum(len(audit.missing) for audit in audits)
    if args.recover:
        print(f"回収した行: {recovered:,} 件 (台帳として読めるようになった)")
        return 0
    if missing:
        logger.error("取りこぼしが %d 件ある。--recover を付けると台帳へ回収する", missing)
        return 1
    return 0


def _run_detail(
    args: argparse.Namespace,
    ledger_cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area] | None,
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
    areas: Sequence[Area] | None,
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


def _run_update(
    args: argparse.Namespace,
    ledger_cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area] | None,
) -> int:
    """月次の差分更新 (ADR 0018)。

    台帳は先に ``fetch-ledger`` で取り直しておく。ここは「前回の出力と突き合わせ、
    取り直すぶんだけ取って書き直す」ところだけを受け持つ。
    """
    summaries = summarize(ledger_cache, categories, areas)
    if not any(summary.unique_key_count for summary in summaries):
        logger.error("台帳が空。先に heritage-crawler fetch-ledger を実行する")
        return 1
    print(format_summary(summaries))

    try:
        existing = read_existing(args.output_dir, datasets_for(categories))
    except UpdateError as error:
        logger.error("%s", error)
        return 1
    if not existing.records:
        logger.error(
            "前回の出力が %s に無い。差分の基準が無いので build-records で全件を組み立てる",
            args.output_dir,
        )
        return 1

    now = datetime.now(UTC)
    slot = args.slot or _slot_of(now)
    # 前回の台帳があれば CSV 同士を突き合わせる (ADR 0020)。無くても追加・削除は
    # 出力との突き合わせで分かるので、落ちるのは「値が変わった」の検出だけ。
    diff: LedgerDiff | None = None
    if args.previous_ledger:
        diff = compare_ledgers(args.previous_ledger, ledger_cache.ledger_dir, categories)
        print(format_ledger_diff(diff))
    else:
        logger.warning(
            "前回の台帳が渡されていない。台帳の値が変わったぶんは見つけられない "
            "(追加・削除と巡回はそのまま働く)"
        )
    plan = plan_update(
        existing,
        read_ledger_rows(ledger_cache, categories, areas),
        diff=diff,
        slot=slot - 1,
        complete_categories={
            summary.category.code for summary in summaries if summary.looks_complete
        },
    )
    print(format_plan(plan, existing))
    for summary in summaries:
        if not summary.looks_complete:
            logger.warning(
                "%s は網羅性を確かめられない (%s)。消えた指定を落とさずに進める",
                summary.category.code,
                summary.note,
            )
    if args.dry_run:
        logger.info("--dry-run なので取得も書き出しもしない")
        return 0
    if plan.is_empty:
        logger.info("取り直すものも落とすものも無い")

    detail_cache = DetailCache(args.cache_dir)
    limiter = RateLimiter(args.interval)

    def client() -> PoliteClient:
        return PoliteClient(contact=args.contact, timeout=args.timeout, limiter=limiter)

    if plan.targets:
        # 取り直すと決めたぶんは、キャッシュに残っていても取り直す (それが目的)。
        fetchers = [client() for _ in range(args.concurrency)]
        try:
            fetch_details(fetchers, detail_cache, plan.targets, force=True)
        except KeyboardInterrupt:
            logger.warning("中断した。書き出していないので、同じコマンドでやり直せる")
            return 130
        except (FetchError, DetailError, LedgerError) as error:
            logger.error("%s", error)
            return 1
        # 取り直したぶんの結果だけを見る (失敗は前回の行が残るので、行は消えない)。
        wanted = {target.key for target in plan.targets}
        failures = [
            entry
            for entry in detail_cache.failures()
            if detail_key(entry.category_code, entry.kanri_taishou_id) in wanted
        ]
        print(format_detail_summary(summarize_details(detail_cache, plan.targets), failures))

    # 落とす前に、その 1 件がデータベースから消えているかを直接確かめる (ADR 0021)。
    # 台帳からの不在は消極的な証拠でしかなく、とくに 102 は網羅性を確かめられない。
    if (probes := candidates(plan, existing)) and plan.control is not None:
        try:
            found = probe_presence(client(), probes, plan.control)
        except KeyboardInterrupt:
            logger.warning("中断した。書き出していないので、同じコマンドでやり直せる")
            return 130
        verify_removals(plan, found)
        print(format_verification(plan))

    report = build_dataset(
        ledger_cache,
        detail_cache,
        categories,
        areas,
        args.output_dir,
        reuse=reuse_for(plan, existing, now.isoformat(timespec="seconds")),
        # **取り直すものが無かった回にも書く** (ADR 0023)。ここまで来ていれば台帳は
        # 取り直せている = データベースを見にいけている。
        checked=Checked(date=args.checked_date or checked_today(now), run_url=args.run_url),
    )
    print(format_report(report))
    return 0


def _run_render_readme(args: argparse.Namespace) -> int:
    """外部へも相手先へも出ない。書き出し済みのデータと README だけを読む。

    ``--check`` であるべき表を出力するのは、月次のドリフト検知が結果をそのまま
    Issue に載せられるようにするため (``.github/workflows/weekly.yml``)。
    """
    try:
        block = render_block(read_counts(args.output_dir))
        current = args.readme.read_text(encoding="utf-8")
        updated = replace_block(current, block)
    except (ReadmeError, OSError) as error:
        logger.error("%s", error)
        return 1

    if updated == current:
        print(f"{args.readme} の件数表はデータと一致している")
        return 0
    if args.check:
        print(block)
        logger.error(
            "%s の件数表がデータとずれている。heritage-crawler render-readme で作り直す",
            args.readme,
        )
        return 1
    args.readme.write_text(updated, encoding="utf-8")
    print(f"{args.readme} の件数表を書き直した")
    return 0


def _fetch_detail(args: argparse.Namespace, cache: DetailCache, targets: Sequence[Target]) -> int:
    if not targets:
        logger.error("台帳が空。先に heritage-crawler fetch-ledger を実行する")
        return 1

    wanted = list(targets)
    if args.recheck_cache:
        # 通信しない検査。ここで未取得へ戻したぶんは、そのまま下の取得で拾われる。
        recheck_cache(cache, wanted)
    if args.retry_failed:
        failed = {
            detail_key(entry.category_code, entry.kanri_taishou_id) for entry in cache.failures()
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
