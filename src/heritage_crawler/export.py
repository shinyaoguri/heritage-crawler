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
    KIND_SPLIT_CATEGORIES,
    TARGET_DATASETS,
    Area,
    Category,
    Dataset,
    datasets_for,
    datasets_of,
    search_areas,
)
from heritage_crawler.detail_page import DetailPage, ParseError, parse_detail_page
from heritage_crawler.ledger import LedgerRow, read_ledger_rows
from heritage_crawler.metadata import (
    accessed_date,
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
    category_of,
    resolve_location,
    routing_kinds,
)
from heritage_crawler.status import build_status, status_path, write_status

logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR: Final = Path("data")
PROGRESS_EVERY: Final = 1000


@dataclass(frozen=True)
class Checked:
    """データベースを見にいったことの記録 (ADR 0023)。

    渡すと、書き出したデータセットごとに ``status.json`` を書く。**渡さなければ
    書かない** — ``build-records`` はキャッシュを読み直すだけで、相手先を見に
    いっていない。実行のたび日付が動く値をここだけに閉じ込めることで、
    ``meta.json`` と JSON Lines は「同じ入力なら同じバイト列」のまま保つ。
    """

    date: str
    """確認日 (``YYYY-MM-DD``、日本時間)。"""

    run_url: str = ""
    """この実行への URL。手元での組み立てには無いので既定は空。"""


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

    removals: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """キー → ``removed.jsonl`` の 1 行 (ADR 0021)。台帳から消えて落とした行。"""

    previous_repos: Mapping[str, set[str]] = field(default_factory=dict)
    """キー → 前回そのキーが居たリポジトリ名。

    落とした行を**前回の居場所へ**書くために要る。あわせて、台帳には居るのに
    振り分けが変わった行 (401 の種別変更、102 の区分変更) もここから分かる。
    """

    previous_removed: Mapping[str, Mapping[str, Mapping[str, Any]]] = field(default_factory=dict)
    """リポジトリ名 → キー → 前回の ``removed.jsonl`` の 1 行。

    **そのまま引き継ぐ。** 消えたままの週に書き直すと ``missing_since`` が動いて
    「いつ消えたか」が失われる。
    """


def build_dataset(
    ledger_cache: LedgerCache,
    detail_cache: DetailCache,
    categories: Sequence[Category],
    areas: Sequence[Area] | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    reuse: Reuse | None = None,
    checked: Checked | None = None,
) -> BuildReport:
    """キャッシュ済みの台帳と詳細から JSON Lines を組み立てて書き出す。

    詳細ページが未取得の行は数えて飛ばす。1 件のパース失敗でも止めない
    (2 万件のうち 1 件で全体が止まると、直すまで何も出力できない)。

    ``reuse`` を渡すと、キャッシュから組み立てられない行を前回の出力で埋める
    (差分更新。ADR 0018)。``checked`` を渡すと ``status.json`` も書く
    (確認した日の記録。ADR 0023)。
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
        fetched_at = _fetched_at(detail_cache, row.category.code, row.get("管理対象ID"))
        for dataset in _datasets_of(row.category, built.record, report):
            groups[(dataset, built.location.area)].append(built.record)
            labels[dataset].update(built.labels)
            latest_fetch[dataset] = max(latest_fetch[dataset], fetched_at)
        report.built += 1
        if report.built % PROGRESS_EVERY == 0:
            logger.info("%d 件組み立てた", report.built)

    for record in reuse.retained if reuse else ():
        # 台帳に出なかった行。分類は行が名乗る分類コードから戻す (ADR 0024)。
        category = category_of(record)
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

    # **消したファイルも「行が動いた」** — 利用日を据え置くとその週だけ日付が止まる。
    touched |= _settle_stale_files(output_dir, categories, groups, report, areas=areas)
    touched |= _write_removed(output_dir, categories, groups, report, reuse=reuse, seen=seen)

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
        if checked is None:
            continue
        # **行が動かなかったデータセットにも書く** (ADR 0023)。確かめたことを残すのが
        # このファイルの役目で、書かずに済ませると「静か」と「止まっている」が
        # また見分けられなくなる。件数と利用日は meta.json と同じ値を渡す。
        status = status_path(output_dir, dataset)
        write_status(
            status,
            build_status(
                dataset,
                checked_date=checked.date,
                accessed_date=payload["source"]["accessed_date"],
                changed=dataset in touched,
                records=payload["counts"]["records"],
                run_url=checked.run_url,
            ),
        )
        report.files.append(f"{status} (確認日 {checked.date})")

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
    # キャッシュは分類コードで引く。台帳ID には複数の分類が同居する (#74)。
    category_code, managed_id = row.category.code, row.get("管理対象ID")
    fetched = detail_cache.is_done(category_code, managed_id)
    if fetched:
        page = _read_page(detail_cache, category_code, managed_id, report)
        if page is not None:
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


def _fetched_at(cache: DetailCache, category_code: str, managed_id: str) -> str:
    """この 1 件を取得した日時 (ISO 8601 の UTC)。記録が無ければ空。

    出典表記の利用日はここから決まる (ADR 0014)。**取得の記録が唯一の実測値**で、
    組み立てを走らせた日時では代用できない。
    """
    entry = cache.entries.get(detail_key(category_code, managed_id))
    return entry.fetched_at if entry else ""


def _read_page(
    cache: DetailCache, category_code: str, managed_id: str, report: BuildReport
) -> DetailPage | None:
    """キャッシュ済みの生 HTML を読む。読めなければ報告に積んで None (呼び手が続ける)。"""
    try:
        html = cache.read_html(category_code, managed_id).decode("utf-8")
        return parse_detail_page(html)
    except (ParseError, UnicodeDecodeError, OSError) as error:
        report.parse_failures.append(f"{category_code}/{managed_id}: {error}")
        return None


def _group_order(group: tuple[Dataset, Area]) -> tuple[int, str]:
    dataset, area = group
    return (TARGET_DATASETS.index(dataset), area.code)


def _settle_stale_files(
    output_dir: Path,
    categories: Sequence[Category],
    groups: dict[tuple[Dataset, Area], list[dict[str, Any]]],
    report: BuildReport,
    *,
    areas: Sequence[Area] | None,
) -> set[Dataset]:
    """今回書かなかった既存ファイルを、消すか報せるかに振り分ける (#57)。

    **0 件の県にはそもそもファイルが無い**のが出力の形 (ADR 0009 / ADR 0013)。
    行が全部無くなったのに書き直されないと、解除された指定が残り続ける。

    ただし 0 件の理由は「データが無くなった」とは限らず、「取れていない」ことも
    ある。**断言できる実行でだけ消す** — それ以外は今までどおり報せるに留める。
    消したデータセットを返すのは、利用日の据え置き判定に使うため (ADR 0020)。
    """
    written = {output_path(output_dir, dataset, area) for dataset, area in groups}
    # ① 全域を見ていない実行では、0 件の県と見ていない県の区別が付かない。
    # ② 詳細を取りこぼした実行では、行が落ちただけの県を消しかねない
    #    (200 で返るエラーページを掴んだ回も同じ。ADR 0011)。
    full_coverage = areas is None or all(
        set(areas) >= set(search_areas(category)) for category in categories
    )
    decisive = full_coverage and not report.missing_html and not report.parse_failures
    produced = {dataset for dataset, _ in groups}
    emptied: set[Dataset] = set()
    for dataset in datasets_for(categories):
        directory = output_dir / dataset.repo / "data"
        if not directory.is_dir():
            continue
        stale = [path for path in sorted(directory.glob("*.jsonl")) if path not in written]
        # ③ その種別に 1 件も書いていない実行 (台帳の取得が途中で止まった等) では、
        #    リポジトリを丸ごと空にしてしまう。台帳に行が無いと ② では捕まらない。
        if decisive and dataset in produced:
            for path in stale:
                path.unlink()
                report.removed_files.append(str(path))
                emptied.add(dataset)
            continue
        report.stale_files.extend(str(path) for path in stale)
    return emptied


REMOVED_FILENAME: Final = "removed.jsonl"
"""落とした行の記録 (ADR 0021)。**データリポジトリのルートに置く。**

``data/`` の下ではない — 閲覧サイトが読むのも配布物に入るのも配信前の検査が
見るのも ``data/*.jsonl`` だけなので、ルートに置けば ``code4heritage/heritages``
は自動的に無視する。``data/`` へ置くと「``meta.json`` に無いのに置かれている
ファイル」として配信が止まる。
"""


def removal_entry(
    record: Mapping[str, Any],
    *,
    conclusion: str,
    last_seen_date: str,
    missing_since: str,
    absent_from_ledger: bool = True,
    category_complete: bool | None = True,
    detail_page: str = "unknown",
    matched_elsewhere: list[str] | None = None,
) -> dict[str, Any]:
    """``removed.jsonl`` の 1 行を組み立てる (ADR 0021)。

    **判定を 1 語に潰さない。** 読む側は ``conclusion`` だけ見てもよいし、
    ``evidence`` に自分の基準を当ててもよい。確実でないものを確実だと書かずに
    済ませるための形。

    レコードは最後に観測したものを丸ごと入れる — 名称も所在地も座標も無い
    削除リストは、事実上「番号の列」でしかない。

    ``missing_since`` は**台帳から消えているのを最初に観測した日**であって、
    解除の告示日ではない (ソースは告示日を持たない)。
    """
    return {
        **record,
        "last_seen_date": last_seen_date,
        "missing_since": missing_since,
        "evidence": {
            "absent_from_ledger": absent_from_ledger,
            "category_complete": category_complete,
            "detail_page": detail_page,
            "matched_elsewhere": matched_elsewhere,
        },
        "conclusion": conclusion,
    }


def _write_removed(
    output_dir: Path,
    categories: Sequence[Category],
    groups: dict[tuple[Dataset, Area], list[dict[str, Any]]],
    report: BuildReport,
    *,
    reuse: Reuse | None,
    seen: set[str],
) -> set[Dataset]:
    """``removed.jsonl`` を書く (ADR 0021)。返すのは記録が動いたデータセット。

    **状態型** — 並ぶのは「いま消えているもの」だけで、復活すれば行は消える。
    誤検出が「解除された文化財」として固定されないための性質で、消えて戻った
    経緯は git 履歴とリリースノート (ADR 0019) が持つ。

    **``reuse`` が無いときは何もしない。** ``build-records`` の全件再組み立ては
    キャッシュしか読まないので履歴を再現できず、書けば消すことになる
    (ADR 0021 の「影響」)。
    """
    if reuse is None:
        return set()

    by_dataset: dict[Dataset, set[str]] = defaultdict(set)
    for (dataset, _), records in groups.items():
        by_dataset[dataset].update(f"{item['ledger_id']}/{item['managed_id']}" for item in records)
    # 走査しながら書き足さないよう、ここで固定する (既定値で穴が開くのを防ぐ)。
    written: Mapping[Dataset, set[str]] = dict(by_dataset)

    moved: set[Dataset] = set()
    for dataset in datasets_for(categories):
        here = written.get(dataset, set())
        previous = reuse.previous_removed.get(dataset.repo, {})
        entries = {
            # ① 今回そのリポジトリへ書いたキーは外す (台帳へ戻った行・振り分けが
            #    戻った行の両方をこれ 1 つで拾える)。
            key: dict(entry)
            for key, entry in previous.items()
            if key not in here
        }
        restored = len(previous) - len(entries)
        # ② 台帳から消えて落とした行を、前回の居場所へ足す。
        for key, entry in reuse.removals.items():
            if dataset.repo in reuse.previous_repos.get(key, set()):
                entries[key] = dict(entry)
        # ③ 台帳には居るのに、そのリポジトリからは消えた行 (401 の種別変更、
        #    102 の区分変更)。利用者から見れば削除と区別が付かない。
        for key in seen & reuse.previous_repos.keys():
            if dataset.repo not in reuse.previous_repos[key] or key in here or key in entries:
                continue
            record = reuse.records.get(key)
            # 書き先が 1 つも無いのは移動ではなく振り分けの失敗 (401 の unroutable)。
            # 報告には出ているので、ここで「移動した」と書かない。
            elsewhere = sorted(other.repo for other in written if key in written[other])
            if record is None or not elsewhere:
                continue
            entries[key] = removal_entry(
                record,
                conclusion="rerouted",
                last_seen_date=reuse.accessed_dates.get(dataset.repo, ""),
                missing_since=accessed_date(reuse.accessed_at),
                absent_from_ledger=False,
                category_complete=None,
                matched_elsewhere=elsewhere,
            )

        report.removed_records += sum(1 for key in entries if key not in previous)
        report.restored_records += restored
        if _put_removed(output_dir / dataset.repo / REMOVED_FILENAME, entries, report):
            moved.add(dataset)
    return moved


def _put_removed(path: Path, entries: dict[str, dict[str, Any]], report: BuildReport) -> bool:
    """記録を書き出す。0 件ならファイルごと消す。動いたかどうかを返す。

    「0 件 = ファイルが無い」は出力の不変条件 (#57 / ADR 0013)。空ファイルを
    残すと「まだ調べていない」と見分けが付かない。
    """
    ordered = sorted(entries.values(), key=lambda entry: (entry["ledger_id"], entry["managed_id"]))
    lines = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in ordered)
    data = lines.encode("utf-8")
    if not data:
        if not path.exists():
            return False
        path.unlink()
        return True
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, data)
    report.files.append(f"{path} ({len(ordered):,} 行)")
    return True


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
    if report.removed_records:
        lines.append(f"落とした行を記録した: {report.removed_records:,} 件 ({REMOVED_FILENAME})")
    if report.restored_records:
        lines.append(f"記録から外した行 (復活): {report.restored_records:,} 件")
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
    lines.extend(f"  0 件になったので消した: {path}" for path in report.removed_files)
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
