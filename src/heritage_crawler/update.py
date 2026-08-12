"""差分更新層 — 前回の出力と台帳を突き合わせ、取り直すぶんだけを決める (ADR 0018)。

**前回の状態はデータリポジトリの JSON Lines そのもの。** キャッシュも生 HTML も
月次実行へ持ち回らない (Actions のキャッシュは 7 日で消え、生 HTML は
リポジトリにコミットしない。ADR 0006)。

取り直すのは 3 種類だけ。

1. 台帳に現れた新しいキー (新規指定)
2. 台帳の値が前回の出力と食い違うキー
3. 巡回のぶん — 全体の 1/12

3 が要るのは、**ソース側に更新日が無く、詳細ページだけの項目 (解説文・員数・
構造及び形式等など) の変更は取り直して比べるしか捕まえられない**ため。
全件を毎月取り直すのは相手先に対して重いので、12 か月で一巡させる。

台帳から消えたキーは指定解除として落とすが、**その分類の網羅性が確かめられて
いるときに限る** (``CategorySummary.looks_complete``)。取得の失敗や相手側の
一時的な障害を指定解除と誤認して行を消さないため。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Final

from heritage_crawler.catalog import CATEGORIES_BY_CODE, Dataset
from heritage_crawler.detail import Target
from heritage_crawler.export import Reuse
from heritage_crawler.ledger import LedgerRow
from heritage_crawler.metadata import METADATA_FILENAME
from heritage_crawler.record import display_label, squeezed

logger = logging.getLogger(__name__)

ROTATION_MONTHS: Final = 12
"""巡回を一巡させる月数。1 か月あたり全体の 1/12 を取り直す (ADR 0018)。"""


class UpdateError(RuntimeError):
    """前回の状態を読めない。差分の基準が無いまま進めない。"""


class Compare(Enum):
    """台帳の列と出力の値をどう比べるか。"""

    TEXT = "text"
    """空白を無視して文字列で比べる (CSV は全角、詳細ページは半角のことがある)。"""

    NUMBER = "number"
    """数値として比べる。緯度経度は CSV から採った値がそのまま入っている。"""

    LIST = "list"
    """同じキーへ順に足して配列で比べる (種別１・種別２ → types)。"""


@dataclass(frozen=True)
class LedgerField:
    key: str
    compare: Compare = Compare.TEXT


LEDGER_FIELDS: Final[dict[str, LedgerField]] = {
    "名称": LedgerField("name"),
    "棟名": LedgerField("ridge_name"),
    "所在地": LedgerField("address"),
    "所有者名": LedgerField("owner"),
    "時代": LedgerField("period"),
    "緯度": LedgerField("latitude", Compare.NUMBER),
    "経度": LedgerField("longitude", Compare.NUMBER),
    "種別1": LedgerField("types", Compare.LIST),
    "種別2": LedgerField("types", Compare.LIST),
}
"""台帳の列と、出力レコードの対応するキー。**一致率を実測して選んだ** (Issue #10)。

出力に入っているのは詳細ページ由来の値で、CSV から採っているのは緯度経度だけ
(ADR 0008)。それでも同じ意味の欄はほぼ一致するので、取り直さずに変更を
見つけられる。手元の 23,742 件で 99.9% 以上一致した列だけをここに置いている。

**都道府県は入れない。** 出力側は正規化済みで、``２県以上`` や未正規化の値が
入った行は ``prefecture`` を持たない (実測で一致率 95.8%)。所在地の比較で足りる。
"""

CATEGORY_FIELDS: Final[dict[str, dict[str, LedgerField]]] = {
    # 102 の 種別1 列に入っているのは種別ではなく国宝・重文区分 (CLAUDE.md の
    # 「CSV の列は分類によって意味が変わる」)。**振り分け先リポジトリを決める値**
    # なので、比べられると国宝への昇格を翌月に捕まえられる (ADR 0009)。
    "102": {**LEDGER_FIELDS, "種別1": LedgerField("national_treasure_class")},
}
"""分類ごとの読み替え。ここに無い分類は ``LEDGER_FIELDS`` をそのまま使う。"""


def rotation_slot(key: str, months: int = ROTATION_MONTHS) -> int:
    """``(台帳ID, 管理対象ID)`` を巡回の枠に割り当てる (ADR 0018)。

    ハッシュで決めるので、都道府県にも種別にも取得順にも偏らない。**実行ごとに
    同じ答えを返す必要がある**ので、実行のたびに種が変わる組み込みの ``hash`` は
    使わない。

    >>> rotation_slot("401/00003452")
    1
    >>> 0 <= rotation_slot("101/00015157") < 12
    True
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % months


def ledger_changes(row: LedgerRow, record: Mapping[str, Any]) -> list[str]:
    """台帳の 1 行と前回の出力で食い違う欄の名前を返す。一致していれば空。

    **CSV 側が空の欄は比べない。** 一覧から回収した行 (ADR 0017) は所在地・
    時代・種別を持たず、比べると毎月「変わった」ことになってしまう。欄が消える
    変更は取りこぼすが、それは巡回で捕まえる。
    """
    fields = CATEGORY_FIELDS.get(row.category.code, LEDGER_FIELDS)
    changes: list[str] = []
    listed: dict[str, list[str]] = {}
    for column, mapped in fields.items():
        raw = row.get(column)
        if not raw:
            continue
        if mapped.compare is Compare.LIST:
            listed.setdefault(mapped.key, []).append(raw)
        elif mapped.compare is Compare.NUMBER:
            if not _same_number(raw, record.get(mapped.key)):
                changes.append(column)
        elif squeezed(raw) != squeezed(str(record.get(mapped.key, ""))):
            changes.append(column)

    for key, values in listed.items():
        if values != [str(value) for value in record.get(key, [])]:
            # 種別1 / 種別2 はまとめて 1 つの欄として報せる (どちらが増えても同じ話)。
            changes.append(_list_label(fields, key))
    return changes


def _list_label(fields: Mapping[str, LedgerField], key: str) -> str:
    """配列へ寄せた列の呼び名。番号だけが違う欄は 1 つに畳む (種別1 / 種別2 → 種別)。"""
    columns = [column for column, mapped in fields.items() if mapped.key == key]
    return display_label(columns[0]) if columns else key


def _same_number(raw: str, value: Any) -> bool:
    """CSV の文字列と出力の数値を比べる。数値として読めない値は「無い」と同じ扱い。"""
    try:
        number = float(raw)
    except ValueError:
        return value is None
    return isinstance(value, int | float) and float(value) == number


@dataclass(frozen=True)
class Existing:
    """前回の出力 (データリポジトリの JSON Lines と ``meta.json``)。"""

    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    """``(台帳ID, 管理対象ID)`` → レコード。

    401 の複合指定は 2 つのリポジトリに同じ行が出るので (ADR 0012)、キーで畳む。
    """

    labels: dict[str, dict[str, str]] = field(default_factory=dict)
    """リポジトリ名 → ``meta.json`` の表示名。"""

    files: int = 0


def record_key(record: Mapping[str, Any]) -> str:
    return f"{record['ledger_id']}/{record['managed_id']}"


def read_existing(output_dir: Path, datasets: Sequence[Dataset]) -> Existing:
    """データリポジトリから前回の状態を読み戻す。

    **読めない行は例外にする。** 黙って飛ばすと、その行は「台帳にあって手元に
    無い」= 新規として扱われ、悪くすると指定解除として消える。差分の基準が
    壊れているときは進まない方がよい。
    """
    records: dict[str, dict[str, Any]] = {}
    labels: dict[str, dict[str, str]] = {}
    files = 0
    for dataset in datasets:
        directory = output_dir / dataset.repo / "data"
        for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
            files += 1
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    key = record_key(record)
                except (json.JSONDecodeError, KeyError, TypeError) as error:
                    raise UpdateError(f"{path} の {number} 行目を読めない: {error}") from error
                if str(record["ledger_id"]) not in CATEGORIES_BY_CODE:
                    # 台帳ID から分類を戻せないと、消えたときの扱いも書き先も決まらない。
                    raise UpdateError(
                        f"{path} の {number} 行目の台帳ID が知らない分類: {record['ledger_id']!r}"
                    )
                records.setdefault(key, record)
        labels[dataset.repo] = _read_labels(output_dir / dataset.repo / METADATA_FILENAME)
    logger.info("前回の出力を読んだ: %d 件 / %d ファイル", len(records), files)
    return Existing(records=records, labels=labels, files=files)


def _read_labels(path: Path) -> dict[str, str]:
    """``meta.json`` の表示名。無ければ空 (今月組み立てたぶんから作り直される)。"""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        labels = payload.get("labels", {})
    except json.JSONDecodeError as error:
        raise UpdateError(f"{path} を読めない: {error}") from error
    return {str(key): str(value) for key, value in labels.items()}


class Reason(Enum):
    """なぜ詳細ページを取り直すか。"""

    ADDED = "新規"
    CHANGED = "台帳の値が変わった"
    ROTATED = "巡回"


@dataclass(frozen=True)
class Refetch:
    """取り直す 1 件と、その理由。"""

    target: Target
    reason: Reason
    changes: tuple[str, ...] = ()
    """``CHANGED`` のときに食い違った欄。報告に出して、原因を追えるようにする。"""

    @property
    def key(self) -> str:
        return self.target.key


@dataclass
class UpdatePlan:
    """今月どこへ手を入れるか (ADR 0018)。"""

    slot: int
    refetch: list[Refetch] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    """台帳から消え、出力からも落とすキー (指定解除)。"""

    retained: list[str] = field(default_factory=list)
    """台帳から消えたが、網羅性を確かめられないので残すキー。"""

    unchanged: int = 0

    @property
    def targets(self) -> list[Target]:
        return [item.target for item in self.refetch]

    def counts(self) -> dict[Reason, int]:
        return {
            reason: sum(1 for item in self.refetch if item.reason is reason) for reason in Reason
        }

    @property
    def is_empty(self) -> bool:
        """手を入れるところが 1 つも無いか。"""
        return not (self.refetch or self.removed)


def plan_update(
    existing: Existing,
    rows: Iterable[LedgerRow],
    *,
    slot: int,
    complete_categories: Container[str],
) -> UpdatePlan:
    """前回の出力と台帳を突き合わせて、今月の計画を立てる。

    ``slot`` は巡回の枠 (0〜11)、``complete_categories`` は網羅性が確かめられた
    分類コード。**そこに無い分類のキーは、台帳から消えていても落とさない**。
    """
    plan = UpdatePlan(slot=slot)
    seen: set[str] = set()

    for row in rows:
        if row.key in seen:  # 同じ棟が複数の地域の CSV に出る (ADR 0008)
            continue
        seen.add(row.key)
        target = Target(
            daichou_id=row.get("台帳ID"),
            kanri_taishou_id=row.get("管理対象ID"),
            name=" ".join(part for part in (row.get("名称"), row.get("棟名")) if part),
        )
        record = existing.records.get(row.key)
        if record is None:
            plan.refetch.append(Refetch(target, Reason.ADDED))
        elif changes := ledger_changes(row, record):
            plan.refetch.append(Refetch(target, Reason.CHANGED, tuple(changes)))
        elif rotation_slot(row.key) == slot:
            plan.refetch.append(Refetch(target, Reason.ROTATED))
        else:
            plan.unchanged += 1

    for key in existing.records.keys() - seen:
        code = str(existing.records[key]["ledger_id"])
        (plan.removed if code in complete_categories else plan.retained).append(key)
    plan.removed.sort()
    plan.retained.sort()
    return plan


def reuse_for(plan: UpdatePlan, existing: Existing, accessed_at: str) -> Reuse:
    """出力層へ渡す「使い回すぶん」を作る (ADR 0018)。

    **落とすキーだけを外す。** 残りは全部渡し、キャッシュに新しい詳細があるかは
    出力層が見る — 取り直したはずの 1 件が取得に失敗しても、前回の行が残って
    行の消失にはならない。
    """
    dropped = set(plan.removed)
    records = {key: record for key, record in existing.records.items() if key not in dropped}
    return Reuse(
        records=records,
        retained=[records[key] for key in plan.retained],
        labels=existing.labels,
        accessed_at=accessed_at,
    )


_EXAMPLES: Final = 10
"""報告に並べる実例の数。件数だけでは原因を追えないので、少しだけ名指しする。"""


def format_plan(plan: UpdatePlan, existing: Existing) -> str:
    """計画を人が読める形にする。件数だけでなく、なぜ取り直すかまで出す。"""
    counts = plan.counts()
    lines = [
        f"前回の出力 {len(existing.records):,} 件 / 巡回の枠 {plan.slot + 1}/{ROTATION_MONTHS}",
        f"取り直す {len(plan.refetch):,} 件 = "
        + " / ".join(f"{reason.value} {counts[reason]:,}" for reason in Reason),
        f"そのまま {plan.unchanged:,} 件",
    ]
    if plan.removed:
        lines.append(f"落とす (指定解除) {len(plan.removed):,} 件")
        lines.extend(f"  {key}" for key in plan.removed[:_EXAMPLES])
        if len(plan.removed) > _EXAMPLES:
            lines.append(f"  ほか {len(plan.removed) - _EXAMPLES:,} 件")
    if plan.retained:
        lines.append(
            f"台帳から消えたが残す {len(plan.retained):,} 件 "
            "(その分類の網羅性を確かめられていない)"
        )
        lines.extend(f"  {key}" for key in plan.retained[:_EXAMPLES])
    changed = [item for item in plan.refetch if item.reason is Reason.CHANGED]
    if changed:
        lines.append("台帳の値が変わったもの:")
        lines.extend(
            f"  {item.key} {item.target.name} ({'・'.join(item.changes)})"
            for item in changed[:_EXAMPLES]
        )
        if len(changed) > _EXAMPLES:
            lines.append(f"  ほか {len(changed) - _EXAMPLES:,} 件")
    return "\n".join(lines)
