"""出力レコードの組み立て — 原文の項目を決めたスキーマへ移す (ADR 0008)。

**このモジュールがスキーマの正本**。原文ラベルとキーの対応、日付の正規化、
都道府県の決め方、欠損の扱いはすべてここに集める。

要点だけ再掲する (根拠は ADR 0008)。

- 分類ごとに違う原文ラベルでも、意味が同じなら同じキーに寄せる
- 日付は ISO 8601。和暦は残さない
- 重複する項目は詳細ページを正とし、CSV からは緯度経度だけを採る
- 値が空ならキーごと落とす (``null`` も空文字も出さない)
- 対応表に無いラベル・読めない日付・怪しい結合は捨てずに ``BuildReport`` へ
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Final

from heritage_crawler.catalog import (
    NON_PREFECTURE_AREAS,
    PREFECTURES,
    Area,
    Category,
    detail_url,
)
from heritage_crawler.detail_page import DetailPage
from heritage_crawler.ledger import LedgerRow


class Kind(Enum):
    """値をどう移すか。"""

    TEXT = "text"
    DATE = "date"
    LIST = "list"
    """同じキーの配列へ順に足す (種別１・種別２ → types)。"""


@dataclass(frozen=True)
class Field:
    key: str
    kind: Kind = Kind.TEXT


def _date(key: str) -> Field:
    return Field(key, Kind.DATE)


def _list(key: str) -> Field:
    return Field(key, Kind.LIST)


FIELD_KEYS: Final[dict[str, Field]] = {
    # 分類をまたいで共通のもの
    "名称": Field("name"),
    "ふりがな": Field("name_kana"),
    "棟名": Field("ridge_name"),
    "棟名ふりがな": Field("ridge_name_kana"),
    "員数": Field("quantity"),
    "時代": Field("period"),
    "年代": Field("year_text"),
    "西暦": Field("western_year"),
    "面積": Field("area"),
    "構造及び形式等": Field("structure"),
    "創建及び沿革": Field("history"),
    "追加年月日": _date("added_date"),
    "所在都道府県": Field("prefecture"),
    "所在地": Field("address"),
    "保管施設の名称": Field("storage_facility"),
    "所有者名": Field("owner"),
    "所有者種別": Field("owner_type"),
    "管理団体・管理責任者名": Field("custodian"),
    # 種別は 102 が 1 つ、101 と 103 が 2 つに分かれている
    "種別": _list("types"),
    "種別１": _list("types"),
    "種別２": _list("types"),
    # 備考にあたる欄。102 だけ棟札の話が前に付く
    "棟礼、墨書、その他参考となるべき事項": Field("notes"),
    "その他参考となるべき事項": Field("notes"),
    # 指定 (102) / 登録 (101) / 選定 (103) で名前が変わるもの
    "指定番号": Field("designation_number"),
    "登録番号": Field("designation_number"),
    "告示番号": Field("designation_number"),
    "重文指定年月日": _date("designated_date"),
    "登録年月日": _date("designated_date"),
    "選定年月日": _date("designated_date"),
    "重文指定基準１": _list("criteria"),
    "重文指定基準２": _list("criteria"),
    "登録基準１": _list("criteria"),
    "登録基準２": _list("criteria"),
    "選定基準１": _list("criteria"),
    "選定基準２": _list("criteria"),
    "選定基準３": _list("criteria"),
    # 分類に固有のもの
    "登録回": Field("registration_round"),
    "登録告示年月日": _date("announced_date"),
    "国宝・重文区分": Field("national_treasure_class"),
    "国宝指定年月日": _date("national_treasure_date"),
}

DESIGNATION_KINDS: Final[dict[str, str]] = {"101": "登録", "102": "指定", "103": "選定"}
"""``designated_date`` が何の日付かを示す。分類から決まる。"""

ANNEX_KEYS: Final[dict[str, str]] = {"附名称": "name", "附員数": "quantity"}
ANNEX_LABEL: Final = "附指定"
ATTACHMENT_LABEL: Final = "添付ファイル"

KEY_ORDER: Final[tuple[str, ...]] = (
    "ledger_id",
    "managed_id",
    "category_name",
    "url",
    "name",
    "name_kana",
    "ridge_name",
    "ridge_name_kana",
    "quantity",
    "types",
    "period",
    "year_text",
    "western_year",
    "area",
    "structure",
    "history",
    "notes",
    "designation_kind",
    "designation_number",
    "registration_round",
    "national_treasure_class",
    "designated_date",
    "announced_date",
    "national_treasure_date",
    "added_date",
    "criteria",
    "prefecture",
    "address",
    "latitude",
    "longitude",
    "storage_facility",
    "owner",
    "owner_type",
    "custodian",
    "description",
    "detailed_description",
    "annexes",
    "has_attachment",
    "has_photo",
)
"""JSON Lines のキーの並び。順序を固定しないと、同じ内容でも差分が出る。"""

# 都道府県で引けない行の受け皿。検索の分割軸と同じものを使う (ADR 0004 の
# ファイル名がそのまま決まる)。
MULTIPLE_PREFECTURES: Final = NON_PREFECTURE_AREAS[0]
UNSPECIFIED_AREA: Final = NON_PREFECTURE_AREAS[1]
_PREFECTURE_BY_NAME: Final = {area.name: area for area in PREFECTURES}

_DATE_RE: Final = re.compile(r"(\d{4})(?:[.\-/](\d{1,2})(?:[.\-/](\d{1,2}))?)?$")


@dataclass(frozen=True)
class Location:
    """出力ファイルの振り分け先と、そこに至った経緯 (ADR 0008 の 4)。"""

    area: Area
    prefecture: str
    """47 都道府県のいずれか。1 つに決まらなければ空。"""

    from_address: bool
    """所在都道府県では決まらず、所在地から拾ったか。"""


def resolve_location(prefecture: str, address: str) -> Location:
    """所在都道府県 → 所在地 の順に都道府県を決める。

    所在都道府県が都道府県名でない行が実在する (101 の 3 件が ``98`` / ``1``)。
    所在地の文字列は正しいので、そこから拾い直す。

    >>> resolve_location("奈良県", "奈良県奈良市秋篠町").prefecture
    '奈良県'
    >>> resolve_location("1", "神奈川県横浜市磯子区森二丁目481").prefecture
    '神奈川県'
    >>> resolve_location("98", "山梨県北杜市／長野県諏訪郡").area.name
    '２県以上'
    """
    if area := _PREFECTURE_BY_NAME.get(prefecture):
        return Location(area=area, prefecture=area.name, from_address=False)

    found = sorted(
        (address.index(name), name) for name in _PREFECTURE_BY_NAME if name in address
    )
    if len(found) >= 2:
        return Location(area=MULTIPLE_PREFECTURES, prefecture="", from_address=True)
    if found:
        area = _PREFECTURE_BY_NAME[found[0][1]]
        return Location(area=area, prefecture=area.name, from_address=True)
    return Location(area=UNSPECIFIED_AREA, prefecture="", from_address=False)


def normalize_date(raw: str) -> str | None:
    """``2004.11.08(平成16.11.08)`` → ``2004-11-08``。読めなければ None。

    和暦は西暦の言い換えなので落とす (ADR 0008)。月や日が無い表記は、その桁まで
    の ISO 8601 にする。

    >>> normalize_date("2004.11.08(平成16.11.08)")
    '2004-11-08'
    >>> normalize_date("1976.09")
    '1976-09'
    >>> normalize_date("平成16年")
    """
    head = re.split(r"[(（]", raw, maxsplit=1)[0].strip()
    matched = _DATE_RE.fullmatch(head)
    if matched is None:
        return None
    year, month, day = matched.groups()
    parts = [year] + [f"{int(part):02d}" for part in (month, day) if part is not None]
    return "-".join(parts)


@dataclass
class BuildReport:
    """組み立ての結果と、黙って捨てなかったもの (ADR 0008 の 8)。"""

    total: int = 0
    built: int = 0
    missing_html: int = 0
    parse_failures: list[str] = field(default_factory=list)
    unknown_labels: Counter[str] = field(default_factory=Counter)
    invalid_dates: Counter[str] = field(default_factory=Counter)
    name_mismatches: list[str] = field(default_factory=list)
    prefecture_from_address: int = 0
    prefecture_unresolved: int = 0
    missing_annexes: int = 0
    missing_treasure_class: int = 0
    """国宝・重文区分が読めず、重要文化財側のリポジトリへ送った棟 (ADR 0009)。"""
    files: list[str] = field(default_factory=list)
    stale_files: list[str] = field(default_factory=list)

    @property
    def has_anomalies(self) -> bool:
        return bool(
            self.parse_failures
            or self.unknown_labels
            or self.invalid_dates
            or self.name_mismatches
            or self.missing_annexes
            or self.prefecture_unresolved
            or self.missing_treasure_class
        )


@dataclass(frozen=True)
class Built:
    """組み立てた 1 レコードと、その置き場 (どのファイルへ書くか)。"""

    record: dict[str, Any]
    location: Location


def build_record(row: LedgerRow, page: DetailPage, report: BuildReport) -> Built:
    """台帳の 1 行と詳細ページ 1 枚を 1 レコードにする。

    値は詳細ページを正とし、CSV からは緯度経度だけを採る。CSV の列は分類に
    よって意味が変わるため、名前が一致していても使わない (ADR 0008 の 3)。
    """
    category = row.category
    values: dict[str, Any] = {
        "ledger_id": row.get("台帳ID"),
        "managed_id": row.get("管理対象ID"),
        "category_name": category.name,
        "url": detail_url(row.get("台帳ID"), row.get("管理対象ID")),
        "designation_kind": DESIGNATION_KINDS[category.code],
    }

    for label, raw in page.fields.items():
        mapped = FIELD_KEYS.get(label)
        if mapped is None:
            report.unknown_labels[f"{category.code} {label}"] += 1
            continue
        if mapped.kind is Kind.LIST:
            values.setdefault(mapped.key, []).append(raw)
        elif mapped.kind is Kind.DATE:
            if (normalized := normalize_date(raw)) is None:
                report.invalid_dates[f"{label}: {raw}"] += 1
            else:
                values[mapped.key] = normalized
        else:
            values[mapped.key] = raw

    location = resolve_location(values.get("prefecture", ""), values.get("address", ""))
    _set_or_drop(values, "prefecture", location.prefecture)
    if location.from_address:
        report.prefecture_from_address += 1
    if not location.prefecture:
        report.prefecture_unresolved += 1

    # CSV にしか無い項目。空のことがある
    _set_or_drop(values, "latitude", _coordinate(row.get("緯度")))
    _set_or_drop(values, "longitude", _coordinate(row.get("経度")))

    _set_or_drop(values, "description", page.description)
    _set_or_drop(values, "detailed_description", page.detailed_description)
    _set_or_drop(values, "annexes", _annexes(page, category, report))
    for label, present in page.related.items():
        if label == ATTACHMENT_LABEL:
            values["has_attachment"] = present
        elif label != ANNEX_LABEL:
            report.unknown_labels[f"{category.code} 関連情報:{label}"] += 1
    values["has_photo"] = page.has_photo

    csv_name = " ".join(part for part in (row.get("名称"), row.get("棟名")) if part)
    page_name = " ".join(
        part for part in (values.get("name", ""), values.get("ridge_name", "")) if part
    )
    if csv_name and page_name and csv_name != page_name:
        # 結合キーの取り違えは、まず名称のずれとして現れる。
        report.name_mismatches.append(f"{row.key} CSV={csv_name} / 詳細={page_name}")

    unknown = set(values) - set(KEY_ORDER)
    if unknown:  # pragma: no cover - KEY_ORDER の付け忘れを実行時に落とすための保険
        raise ValueError(f"KEY_ORDER に無いキーを作った: {sorted(unknown)}")
    return Built(
        record={key: values[key] for key in KEY_ORDER if key in values},
        location=location,
    )


def _set_or_drop(values: dict[str, Any], key: str, value: Any) -> None:
    """空ならキーごと落とす (ADR 0008 の 6)。"""
    if value:
        values[key] = value
    else:
        values.pop(key, None)


def _coordinate(raw: str) -> float | None:
    try:
        return float(raw)
    except ValueError:
        return None


def _annexes(page: DetailPage, category: Category, report: BuildReport) -> list[dict[str, str]]:
    annexes = []
    for raw in page.annexes:
        annex = {}
        for label, value in raw.items():
            key = ANNEX_KEYS.get(label)
            if key is None:
                report.unknown_labels[f"{category.code} 附指定:{label}"] += 1
                continue
            if value:
                annex[key] = value
        if annex:
            annexes.append(annex)
    if page.related.get(ANNEX_LABEL) and not annexes:
        # 「附指定あり」と書いてあるのに一覧が読めていない。取りこぼしを疑う。
        report.missing_annexes += 1
    return annexes
