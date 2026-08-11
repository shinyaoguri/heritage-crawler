"""出力層 — キャッシュを読んで都道府県ごとの JSON Lines を書く (ADR 0004)。

外部へは一切出ない。台帳 CSV と生 HTML のキャッシュだけを読むので、スキーマを
変えても 2 万件の取り直しは要らない (ADR 0006)。

出力先は ``<出力ディレクトリ>/<分類コード>/<都道府県コード>_<ローマ字>.jsonl``。
行は ``(台帳ID, 管理対象ID)`` で安定ソートし、取得順に依存させない。
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from heritage_crawler.cache import DetailCache, LedgerCache, atomic_write
from heritage_crawler.catalog import SEARCH_AREAS, Area, Category
from heritage_crawler.detail_page import DetailPage, ParseError, parse_detail_page
from heritage_crawler.ledger import read_ledger_rows
from heritage_crawler.record import BuildReport, build_record

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR: Final = Path("data")
PROGRESS_EVERY: Final = 1000


def build_dataset(
    ledger_cache: LedgerCache,
    detail_cache: DetailCache,
    categories: Sequence[Category],
    areas: Sequence[Area] = SEARCH_AREAS,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> BuildReport:
    """キャッシュ済みの台帳と詳細から JSON Lines を組み立てて書き出す。

    詳細ページが未取得の行は数えて飛ばす。1 件のパース失敗でも止めない
    (2 万件のうち 1 件で全体が止まると、直すまで何も出力できない)。
    """
    report = BuildReport()
    groups: dict[tuple[Category, Area], list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()

    for row in read_ledger_rows(ledger_cache, categories, areas):
        # 同じ棟が複数の地域の CSV に出る (ADR 0008 の結合キー)。先に見た方を採る。
        if row.key in seen:
            continue
        seen.add(row.key)
        report.total += 1

        page = _read_page(detail_cache, row.get("台帳ID"), row.get("管理対象ID"), report)
        if page is None:
            continue
        built = build_record(row, page, report)
        groups[(row.category, built.location.area)].append(built.record)
        report.built += 1
        if report.built % PROGRESS_EVERY == 0:
            logger.info("%d 件組み立てた", report.built)

    for (category, area), records in sorted(groups.items(), key=lambda item: _group_order(item[0])):
        path = output_path(output_dir, category, area)
        records.sort(key=lambda record: (record["ledger_id"], record["managed_id"]))
        lines = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, lines.encode("utf-8"))
        report.files.append(f"{path} ({len(records):,} 行)")

    _note_stale_files(output_dir, categories, groups, report)
    return report


def output_path(output_dir: Path, category: Category, area: Area) -> Path:
    """ADR 0004 のファイル配置。台帳キャッシュとは区切り文字が違う点に注意。"""
    return output_dir / category.code / f"{area.code}_{area.slug}.jsonl"


def _read_page(
    cache: DetailCache, ledger_id: str, managed_id: str, report: BuildReport
) -> DetailPage | None:
    if not cache.is_done(ledger_id, managed_id):
        report.missing_html += 1
        return None
    try:
        html = cache.read_html(ledger_id, managed_id).decode("utf-8")
        return parse_detail_page(html)
    except (ParseError, UnicodeDecodeError, OSError) as error:
        report.parse_failures.append(f"{ledger_id}/{managed_id}: {error}")
        return None


def _group_order(group: tuple[Category, Area]) -> tuple[str, str]:
    category, area = group
    return (category.code, area.code)


def _note_stale_files(
    output_dir: Path,
    categories: Sequence[Category],
    groups: dict[tuple[Category, Area], list[dict[str, Any]]],
    report: BuildReport,
) -> None:
    """今回書かなかった既存ファイルを報せる。

    地域を絞って走らせるのは普通のことなので消しはしない。ただし黙っていると、
    都道府県の振り分けが変わったときに古い行が残り続ける。
    """
    written = {output_path(output_dir, category, area) for category, area in groups}
    for category in categories:
        directory = output_dir / category.code
        if not directory.is_dir():
            continue
        report.stale_files.extend(
            str(path) for path in sorted(directory.glob("*.jsonl")) if path not in written
        )


def format_report(report: BuildReport) -> str:
    """組み立て結果を人が読める形にする。異常は件数と実例を並べる。"""
    lines = [
        f"対象 {report.total:,} 件 / 出力 {report.built:,} 件 / "
        f"詳細が未取得 {report.missing_html:,} 件",
    ]
    lines.extend(f"  書き出し: {entry}" for entry in report.files)
    if report.missing_html:
        lines.append("未取得ぶんは fetch-detail で取れる")

    lines.extend(_anomaly_lines("読めなかったページ", report.parse_failures))
    lines.extend(_counter_lines("対応表に無いラベル", report.unknown_labels))
    lines.extend(_counter_lines("日付として読めない値", report.invalid_dates))
    lines.extend(_anomaly_lines("CSV と詳細で名称が違う", report.name_mismatches))
    if report.missing_annexes:
        lines.append(f"附指定ありなのに一覧が空: {report.missing_annexes:,} 件")
    if report.prefecture_from_address:
        lines.append(f"都道府県を所在地から決めた: {report.prefecture_from_address:,} 件")
    if report.prefecture_unresolved:
        lines.append(f"都道府県が 1 つに決まらなかった: {report.prefecture_unresolved:,} 件")
    lines.extend(f"  今回書かなかった既存ファイル: {path}" for path in report.stale_files)
    return "\n".join(lines)


def _anomaly_lines(title: str, entries: Sequence[str], limit: int = 5) -> list[str]:
    if not entries:
        return []
    lines = [f"{title}: {len(entries):,} 件"]
    lines.extend(f"  {entry}" for entry in entries[:limit])
    if len(entries) > limit:
        lines.append(f"  ほか {len(entries) - limit:,} 件")
    return lines


def _counter_lines(title: str, counter: dict[str, int], limit: int = 5) -> list[str]:
    if not counter:
        return []
    ranked = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    lines = [f"{title}: {len(counter):,} 種類 / {sum(counter.values()):,} 件"]
    lines.extend(f"  {label} ×{count:,}" for label, count in ranked[:limit])
    if len(ranked) > limit:
        lines.append(f"  ほか {len(ranked) - limit:,} 種類")
    return lines
