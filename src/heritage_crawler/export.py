"""出力層 — キャッシュを読んで都道府県ごとの JSON Lines を書く (ADR 0004)。

外部へは一切出ない。台帳 CSV と生 HTML のキャッシュだけを読むので、スキーマを
変えても 2 万件の取り直しは要らない (ADR 0006)。

出力先は ``<出力ディレクトリ>/<リポジトリ名>/data/<都道府県コード>_<ローマ字>.jsonl``
(ADR 0009)。``--output-dir`` はデータリポジトリを並べた親ディレクトリを指す。
行は ``(台帳ID, 管理対象ID)`` で安定ソートし、取得順に依存させない。
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from heritage_crawler.cache import DetailCache, LedgerCache, atomic_write, detail_key
from heritage_crawler.catalog import (
    KIND_SPLIT_CATEGORIES,
    SEARCH_AREAS,
    TARGET_DATASETS,
    Area,
    Category,
    Dataset,
    datasets_for,
    datasets_of,
)
from heritage_crawler.detail_page import DetailPage, ParseError, parse_detail_page
from heritage_crawler.ledger import read_ledger_rows
from heritage_crawler.metadata import (
    build_metadata,
    generator_version,
    metadata_path,
    write_metadata,
)
from heritage_crawler.record import BuildReport, build_record, routing_kinds

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
    groups: dict[tuple[Dataset, Area], list[dict[str, Any]]] = defaultdict(list)
    labels: dict[Dataset, dict[str, str]] = defaultdict(dict)
    latest_fetch: dict[Dataset, str] = defaultdict(str)
    seen: set[str] = set()

    for row in read_ledger_rows(ledger_cache, categories, areas):
        # 同じ棟が複数の地域の CSV に出る (ADR 0008 の結合キー)。先に見た方を採る。
        if row.key in seen:
            continue
        seen.add(row.key)
        report.total += 1

        ledger_id, managed_id = row.get("台帳ID"), row.get("管理対象ID")
        page = _read_page(detail_cache, ledger_id, managed_id, report)
        if page is None:
            continue
        built = build_record(row, page, report)
        fetched_at = _fetched_at(detail_cache, ledger_id, managed_id)
        for dataset in _datasets_of(row.category, built.record, report):
            groups[(dataset, built.location.area)].append(built.record)
            labels[dataset].update(built.labels)
            latest_fetch[dataset] = max(latest_fetch[dataset], fetched_at)
        report.built += 1
        if report.built % PROGRESS_EVERY == 0:
            logger.info("%d 件組み立てた", report.built)

    written: dict[Dataset, list[tuple[Area, list[dict[str, Any]]]]] = defaultdict(list)
    for (dataset, area), records in sorted(groups.items(), key=lambda item: _group_order(item[0])):
        path = output_path(output_dir, dataset, area)
        records.sort(key=lambda record: (record["ledger_id"], record["managed_id"]))
        lines = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, lines.encode("utf-8"))
        report.files.append(f"{path} ({len(records):,} 行)")
        written[dataset].append((area, records))

    version = generator_version()
    for dataset, entries in written.items():
        path = metadata_path(output_dir, dataset)
        payload = build_metadata(dataset, entries, labels[dataset], latest_fetch[dataset], version)
        write_metadata(path, payload)
        report.files.append(f"{path} (利用日 {payload['source']['accessed_date']})")

    _note_stale_files(output_dir, categories, groups, report)
    return report


def output_path(output_dir: Path, dataset: Dataset, area: Area) -> Path:
    """ADR 0009 のファイル配置。リポジトリ 1 つが文化財の種別 1 つに対応する。"""
    return output_dir / dataset.repo / "data" / f"{area.code}_{area.slug}.jsonl"


def _datasets_of(
    category: Category, record: dict[str, Any], report: BuildReport
) -> list[Dataset]:
    """1 件の書き先リポジトリを決める (ADR 0009 / ADR 0012)。

    **複数返ることがある** — 401 の複合指定は種別を 2 つ持ち、どちらの種別から
    見ても構成員なので両方へ書く。

    区分が読めないときの行き先は分類で違う。102 には受け皿 (重要文化財) が
    あるが 401 には無い。どちらも黙って通すと振り分けの誤りに気付けないので、
    受け皿へ落ちたぶんと書き先が無かったぶんを別々に数える。
    """
    kinds = routing_kinds(category, record)
    if not kinds and category in KIND_SPLIT_CATEGORIES:
        report.missing_kind += 1
    datasets = datasets_of(category, kinds)
    if not datasets:
        report.unroutable.append(
            f"{record['ledger_id']}/{record['managed_id']} {record.get('name', '')}"
        )
    return datasets


def _fetched_at(cache: DetailCache, ledger_id: str, managed_id: str) -> str:
    """この 1 件を取得した日時 (ISO 8601 の UTC)。記録が無ければ空。

    出典表記の利用日はここから決まる (ADR 0014)。**取得の記録が唯一の実測値**で、
    組み立てを走らせた日時では代用できない。
    """
    entry = cache.entries.get(detail_key(ledger_id, managed_id))
    return entry.fetched_at if entry else ""


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


def _group_order(group: tuple[Dataset, Area]) -> tuple[int, str]:
    dataset, area = group
    return (TARGET_DATASETS.index(dataset), area.code)


def _note_stale_files(
    output_dir: Path,
    categories: Sequence[Category],
    groups: dict[tuple[Dataset, Area], list[dict[str, Any]]],
    report: BuildReport,
) -> None:
    """今回書かなかった既存ファイルを報せる。

    地域を絞って走らせるのは普通のことなので消しはしない。ただし黙っていると、
    都道府県の振り分けが変わったときに古い行が残り続ける。
    """
    written = {output_path(output_dir, dataset, area) for dataset, area in groups}
    for dataset in datasets_for(categories):
        directory = output_dir / dataset.repo / "data"
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
    lines.extend(_anomaly_lines("種別が読めず書き先が無かった", report.unroutable))
    lines.extend(_counter_lines("関連情報にありと出ているのに一覧が空", report.missing_rellists))
    if report.missing_kind:
        lines.append(f"区分が読めず受け皿のリポジトリへ送った: {report.missing_kind:,} 件")
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
