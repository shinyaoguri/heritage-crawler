"""出力層 — キャッシュを読んで都道府県ごとの JSON Lines を書く (ADR 0004)。

外部へは一切出ない。台帳 CSV と生 HTML のキャッシュだけを読むので、スキーマを
変えても 2 万件の取り直しは要らない (ADR 0006)。

出力先は ``<出力ディレクトリ>/<リポジトリ名>/data/<都道府県コード>_<ローマ字>.jsonl``
(ADR 0009)。``--output-dir`` はデータリポジトリを並べた親ディレクトリを指す。
行は ``(台帳ID, 管理対象ID)`` で安定ソートし、取得順に依存させない。

差分更新 (ADR 0018) では ``Reuse`` を渡す。詳細を取り直していない行は前回の
出力をそのまま使い、書き出しの仕組みは全件のときと同じものを通る。
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from heritage_crawler.cache import DetailCache, LedgerCache, atomic_write, detail_key
from heritage_crawler.catalog import (
    CATEGORIES_BY_CODE,
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
from heritage_crawler.ledger import LedgerRow, read_ledger_rows
from heritage_crawler.metadata import (
    build_metadata,
    generator_version,
    metadata_path,
    write_metadata,
)
from heritage_crawler.record import (
    KEY_ORDER,
    BuildReport,
    Built,
    build_record,
    resolve_location,
    routing_kinds,
)

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR: Final = Path("data")
PROGRESS_EVERY: Final = 1000


@dataclass(frozen=True)
class Reuse:
    """差分更新で使い回す前回の状態 (ADR 0018)。

    差分の意味づけ (何を新規と見なし、何を巡回で取り直すか) は ``update`` が
    持つ。こちらが知っているのは「取り直していない行は前回の出力をそのまま
    使う」ことだけで、書き出しの仕組みは全件のときと変わらない。
    """

    records: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """``(台帳ID, 管理対象ID)`` → 前回のレコード。**キャッシュが優先**され、
    ここが使われるのは詳細を取り直していない行 (と、取得に失敗した行)。"""

    retained: Sequence[Mapping[str, Any]] = ()
    """台帳に現れなかったが、消さずに残す行。

    網羅性が確かめられない分類で、取得の失敗を指定解除と誤認しないための逃げ道。
    """

    labels: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    """リポジトリ名 → 前回の ``meta.json`` の表示名。

    差分更新では毎月 2,000 件しか組み立てないので、実測できる表示名もその範囲に
    しか現れない。前回のぶんを土台にしないと ``meta.json`` が月ごとに揺れる。
    """

    accessed_at: str = ""
    """行が変わったデータセットの利用日にする日時 (ISO 8601)。

    取り直したぶんの取得日時が分からない場合の拠りどころで、実行日を渡す。
    """

    accessed_dates: Mapping[str, str] = field(default_factory=dict)
    """リポジトリ名 → 前回の ``meta.json`` の利用日 (``YYYY-MM-DD``)。

    **行が 1 つも変わらなかったデータセットは、この日付を据え置く** (ADR 0020)。
    利用日は「そのデータを取り出した日」なので、取り出し直していない回に動かす
    理由が無い。据え置けば `meta.json` も 1 バイトも変わらず、確認しただけの回に
    コミットが立たない。
    """


def build_dataset(
    ledger_cache: LedgerCache,
    detail_cache: DetailCache,
    categories: Sequence[Category],
    areas: Sequence[Area] = SEARCH_AREAS,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reuse: Reuse | None = None,
) -> BuildReport:
    """キャッシュ済みの台帳と詳細から JSON Lines を組み立てて書き出す。

    詳細ページが未取得の行は数えて飛ばす。1 件のパース失敗でも止めない
    (2 万件のうち 1 件で全体が止まると、直すまで何も出力できない)。

    ``reuse`` を渡すと、キャッシュから組み立てられない行を前回の出力で埋める
    (差分更新。ADR 0018)。
    """
    report = BuildReport()
    groups: dict[tuple[Dataset, Area], list[dict[str, Any]]] = defaultdict(list)
    labels: dict[Dataset, dict[str, str]] = defaultdict(dict)
    latest_fetch: dict[Dataset, str] = defaultdict(str)
    seen: set[str] = set()
    if reuse is not None:
        for dataset in datasets_for(categories):
            labels[dataset].update(reuse.labels.get(dataset.repo, {}))

    for row in read_ledger_rows(ledger_cache, categories, areas):
        # 同じ棟が複数の地域の CSV に出る (ADR 0008 の結合キー)。先に見た方を採る。
        if row.key in seen:
            continue
        seen.add(row.key)
        report.total += 1

        built = _built(row, detail_cache, reuse, report)
        if built is None:
            continue
        fetched_at = _fetched_at(detail_cache, row.get("台帳ID"), row.get("管理対象ID"))
        for dataset in _datasets_of(row.category, built.record, report):
            groups[(dataset, built.location.area)].append(built.record)
            labels[dataset].update(built.labels)
            latest_fetch[dataset] = max(latest_fetch[dataset], fetched_at)
        report.built += 1
        if report.built % PROGRESS_EVERY == 0:
            logger.info("%d 件組み立てた", report.built)

    for record in reuse.retained if reuse else ():
        # 台帳に出なかった行。分類は台帳ID から戻せる (分類コードと同じ値)。
        category = CATEGORIES_BY_CODE[str(record["ledger_id"])]
        built = _reused(record)
        for dataset in _datasets_of(category, built.record, report):
            groups[(dataset, built.location.area)].append(built.record)
        report.retained += 1

    written: dict[Dataset, list[tuple[Area, list[dict[str, Any]]]]] = defaultdict(list)
    # 行が 1 バイトでも動いたデータセット。利用日を据え置いてよいかの判定に使う。
    touched: set[Dataset] = set()
    for (dataset, area), records in sorted(groups.items(), key=lambda item: _group_order(item[0])):
        path = output_path(output_dir, dataset, area)
        records.sort(key=lambda record: (record["ledger_id"], record["managed_id"]))
        lines = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
        data = lines.encode("utf-8")
        if not path.exists() or path.read_bytes() != data:
            touched.add(dataset)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, data)
        report.files.append(f"{path} ({len(records):,} 行)")
        written[dataset].append((area, records))

    version = generator_version()
    for dataset, entries in written.items():
        path = metadata_path(output_dir, dataset)
        fetched_at = reuse.accessed_at if reuse else latest_fetch[dataset]
        # **行が動いていないなら利用日も動かさない** (ADR 0020)。取り出し直して
        # いない回に日付だけ進めると、出典表記が実態とずれるうえ、確認しただけの
        # 回に 10 リポジトリぶんのコミットが立つ。
        accessed = ""
        if reuse is not None and dataset not in touched:
            accessed = reuse.accessed_dates.get(dataset.repo, "")
        payload = build_metadata(dataset, entries, labels[dataset], fetched_at, version, accessed)
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


def _built(
    row: LedgerRow, detail_cache: DetailCache, reuse: Reuse | None, report: BuildReport
) -> Built | None:
    """台帳の 1 行をレコードにする。**キャッシュが先、前回の出力は控え**。

    差分更新では大半の行の詳細ページを取り直さないので、キャッシュに無いぶんは
    前回の出力で埋まる。取得に失敗した 1 件も前回の行が残るだけで済み、
    **失敗が行の消失にならない** (ADR 0018)。
    """
    ledger_id, managed_id = row.get("台帳ID"), row.get("管理対象ID")
    fetched = detail_cache.is_done(ledger_id, managed_id)
    if fetched and (page := _read_page(detail_cache, ledger_id, managed_id, report)) is not None:
        return build_record(row, page, report)
    if reuse is not None and (record := reuse.records.get(row.key)) is not None:
        report.reused += 1
        return _reused(record)
    if not fetched:
        report.missing_html += 1
    return None


def _reused(record: Mapping[str, Any]) -> Built:
    """前回の出力の 1 行を、書き出せる形に戻す。

    置き場 (どのファイルへ書くか) は組み立て直しと同じ規則で決める — 前回の
    ファイル名からではなく値から決めるので、都道府県の判定を直せば次の実行で
    正しい場所へ移る。表示名は前回の ``meta.json`` から来るのでここでは持たない。
    """
    values = dict(record)
    location = resolve_location(str(values.get("prefecture", "")), str(values.get("address", "")))
    # 並びは書き出したときのままのはずだが、スキーマの並びを正本として通す。
    ordered = {key: values.pop(key) for key in KEY_ORDER if key in values}
    ordered.update(sorted(values.items()))  # KEY_ORDER に無いキーは落とさず末尾へ
    return Built(record=ordered, location=location)


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
    """キャッシュ済みの生 HTML を読む。読めなければ報告に積んで None (呼び手が続ける)。"""
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
    if report.reused:
        lines.append(f"うち前回の出力をそのまま使った: {report.reused:,} 件")
    if report.retained:
        lines.append(f"台帳に出なかったが残した: {report.retained:,} 件")
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
