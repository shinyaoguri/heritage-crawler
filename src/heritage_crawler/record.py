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

    SPLIT = "split"
    """1 欄に詰まった複数の値を割って配列へ足す (401 の指定基準)。

    建造物系は基準を ``登録基準１`` / ``登録基準２`` と欄で分けるが、401 は
    1 つの欄に半角カンマ区切りで並べる。空要素も混じる。
    """


@dataclass(frozen=True)
class Field:
    key: str
    kind: Kind = Kind.TEXT


def _date(key: str) -> Field:
    return Field(key, Kind.DATE)


def _list(key: str) -> Field:
    return Field(key, Kind.LIST)


def _split(key: str) -> Field:
    return Field(key, Kind.SPLIT)


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
    "所在地（市区町村）": Field("address"),
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
    # 指定 (102 / 401) / 登録 (101) / 選定 (103) で名前が変わるもの
    "指定番号": Field("designation_number"),
    "登録番号": Field("designation_number"),
    "告示番号": Field("designation_number"),
    "重文指定年月日": _date("designated_date"),
    "指定年月日": _date("designated_date"),
    "登録年月日": _date("designated_date"),
    "選定年月日": _date("designated_date"),
    "重文指定基準１": _list("criteria"),
    "重文指定基準２": _list("criteria"),
    "登録基準１": _list("criteria"),
    "登録基準２": _list("criteria"),
    "選定基準１": _list("criteria"),
    "選定基準２": _list("criteria"),
    "選定基準３": _list("criteria"),
    # 401 は基準を欄で分けず、1 欄にカンマ区切りで並べる
    "指定基準": _split("criteria"),
    # 分類に固有のもの
    "登録回": Field("registration_round"),
    "登録告示年月日": _date("announced_date"),
    "国宝・重文区分": Field("national_treasure_class"),
    "国宝指定年月日": _date("national_treasure_date"),
    # 401 の特別指定 (特別史跡・特別名勝・特別天然記念物)
    "特別区分": Field("special_class"),
    "特別指定年月日": _date("special_designated_date"),
}

DESIGNATION_KINDS: Final[dict[str, str]] = {
    "101": "登録",
    "102": "指定",
    "103": "選定",
    "401": "指定",
}
"""``designated_date`` が何の日付かを示す。分類から決まる。"""


ROUTING_KEYS: Final[dict[str, str]] = {
    "102": "national_treasure_class",
    "401": "types",
}
"""どの分類が、どのキーの値でデータリポジトリに振り分けられるか (ADR 0012)。

ここに無い分類は区分を持たず、受け皿のリポジトリ 1 つへ行く。
"""

MEASURES_LABEL: Final = "指定等後に行った措置"
"""関連情報の欄にある 401 固有のラベル。有無だけを ``has_measures`` に採る。"""

ANNEX_KEYS: Final[dict[str, Field]] = {"附名称": Field("name"), "附員数": Field("quantity")}
ANNEX_LABEL: Final = "附指定"
ATTACHMENT_LABEL: Final = "添付ファイル"

MEASURE_KEYS: Final[dict[str, Field]] = {
    "異動年月日": _date("date"),
    "異動種別1": _list("types"),
    "異動種別2": _list("types"),
    "異動種別3": _list("types"),
    "異動内容": Field("note"),
}
"""指定等後に行った措置 1 件ぶんの対応表 (401)。

**同じ ``detail_rellist_*`` モーダルが分類によって別のものを載せる。** 建造物系は
附指定の一覧、401 は指定・追加指定・名称変更などの履歴。ラベルで見分ける
(数字は半角。全角の ``異動種別１`` ではない)。
"""

DERIVED_LABELS: Final[dict[str, str]] = {
    "ledger_id": "台帳ID",
    "managed_id": "管理対象ID",
    "category_name": "分類",
    "url": "詳細ページ",
    "designation_kind": "指定・登録・選定の別",
    "prefecture": "所在都道府県",
    "latitude": "緯度",
    "longitude": "経度",
    "description": "解説文",
    "detailed_description": "詳細解説",
    "annexes": ANNEX_LABEL,
    "measures": MEASURES_LABEL,
    "has_attachment": f"{ATTACHMENT_LABEL}の有無",
    "has_measures": f"{MEASURES_LABEL}の有無",
    "has_photo": "写真の有無",
}
"""原文ラベルを持たないキーの表示名。

``FIELD_KEYS`` の対応表から来ないもの — CSV 由来 (緯度経度)、モーダル由来
(解説文・附指定)、こちらで組み立てたもの (URL・分類) の呼び名をここで決める。
**表示名もスキーマの知識なのでここが正本**にする。持たせないと、データを読む側が
それぞれ独自の和訳を抱えることになり、原文の表記が変わっても追随できない
(ADR 0014)。

原文にも同じ欄があるキー (所在都道府県) も置いてある。分類によっては欄が無く、
こちらで所在地から決めるため — 103 の詳細ページに所在都道府県の欄は無い。
**原文のラベルがあればそちらが優先**で、ここは無かったときの控えになる。
"""

_LABEL_INDEX_RE: Final = re.compile(r"[0-9０-９]+$")


def display_label(label: str) -> str:
    """原文ラベルを表示名にする。番号で分かれた欄は番号を落として 1 つに畳む。

    同じキーへ寄る欄は番号だけが違うことが多い (種別１ / 種別２ → types)。
    畳まないと、どちらの番号が表示名になるかが読み取り順で決まってしまう。

    >>> display_label("種別１")
    '種別'
    >>> display_label("重文指定基準２")
    '重文指定基準'
    >>> display_label("名称")
    '名称'
    """
    return _LABEL_INDEX_RE.sub("", label) or label


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
    "special_class",
    "designated_date",
    "announced_date",
    "national_treasure_date",
    "special_designated_date",
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
    "measures",
    "has_attachment",
    "has_measures",
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
    reused: int = 0
    """詳細を取り直さず、前回の出力をそのまま使ったもの (差分更新。ADR 0018)。"""
    retained: int = 0
    """台帳に現れなかったが、消さずに残したもの (網羅性が確かめられない分類)。"""
    removed_records: int = 0
    """``removed.jsonl`` に新しく記録した行 (ADR 0021)。異常ではない。"""
    restored_records: int = 0
    """記録から外した行 (台帳へ戻った・振り分けが戻った)。"""
    parse_failures: list[str] = field(default_factory=list)
    unknown_labels: Counter[str] = field(default_factory=Counter)
    invalid_dates: Counter[str] = field(default_factory=Counter)
    name_mismatches: list[str] = field(default_factory=list)
    prefecture_from_address: int = 0
    prefecture_unresolved: int = 0
    missing_rellists: Counter[str] = field(default_factory=Counter)
    """関連情報に「あり」と出ているのに、一覧が読めなかったもの (附指定 / 措置)。"""
    missing_kind: int = 0
    """区分が読めず、受け皿のリポジトリへ送った件数 (102 なら重要文化財側。ADR 0009)。"""
    unroutable: list[str] = field(default_factory=list)
    """種別が読めず、どのリポジトリへも書けなかったもの (401 に受け皿は無い。ADR 0012)。"""
    files: list[str] = field(default_factory=list)
    stale_files: list[str] = field(default_factory=list)
    removed_files: list[str] = field(default_factory=list)
    """行が 0 件になったので消したファイル (#57)。異常ではないが、黙って消さない。"""

    @property
    def has_anomalies(self) -> bool:
        return bool(
            self.parse_failures
            or self.unknown_labels
            or self.invalid_dates
            or self.name_mismatches
            or self.missing_rellists
            or self.prefecture_unresolved
            or self.missing_kind
            or self.unroutable
        )


@dataclass(frozen=True)
class Built:
    """組み立てた 1 レコードと、その置き場 (どのファイルへ書くか)。"""

    record: dict[str, Any]
    location: Location
    labels: dict[str, str] = field(default_factory=dict)
    """このレコードで実際に使われた ``キー → 表示名``。

    どの原文ラベルが現れるかは分類で違い、対応表からは決まらない (同じキーに
    複数の原文ラベルが寄っている)。データセットのメタデータに載せる表示名は
    実測するしかないので、組み立てながら拾う (ADR 0014)。
    附指定・措置の中のキーは ``annexes.name`` のように親のキーで修飾する。
    """


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
        # URL の第 1 セグメントは分類コード。CSV の台帳ID 列ではない (#74)。
        "url": detail_url(category.code, row.get("管理対象ID")),
        "designation_kind": DESIGNATION_KINDS[category.code],
    }
    labels: dict[str, str] = {}

    for label, raw in page.fields.items():
        mapped = FIELD_KEYS.get(label)
        if mapped is None:
            report.unknown_labels[f"{category.code} {label}"] += 1
            continue
        if mapped.kind is Kind.LIST:
            values.setdefault(mapped.key, []).append(raw)
        elif mapped.kind is Kind.SPLIT:
            values.setdefault(mapped.key, []).extend(_split_values(raw))
        elif mapped.kind is Kind.DATE:
            if (normalized := normalize_date(raw)) is None:
                report.invalid_dates[f"{label}: {raw}"] += 1
                continue
            values[mapped.key] = normalized
        else:
            values[mapped.key] = raw
        labels[mapped.key] = display_label(label)

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
    annexes, measures = _rellists(page, category, report, labels)
    _set_or_drop(values, "annexes", annexes)
    _set_or_drop(values, "measures", measures)
    for label, present in page.related.items():
        if label == ATTACHMENT_LABEL:
            values["has_attachment"] = present
        elif label == MEASURES_LABEL:
            values["has_measures"] = present
        elif label != ANNEX_LABEL:
            report.unknown_labels[f"{category.code} 関連情報:{label}"] += 1
    values["has_photo"] = page.has_photo

    csv_name = " ".join(part for part in (row.get("名称"), row.get("棟名")) if part)
    page_name = " ".join(
        part for part in (values.get("name", ""), values.get("ridge_name", "")) if part
    )
    if csv_name and page_name and squeezed(csv_name) != squeezed(page_name):
        # 結合キーの取り違えは、まず名称のずれとして現れる。
        report.name_mismatches.append(f"{row.key} CSV={csv_name} / 詳細={page_name}")

    unknown = set(values) - set(KEY_ORDER)
    if unknown:  # pragma: no cover - KEY_ORDER の付け忘れを実行時に落とすための保険
        raise ValueError(f"KEY_ORDER に無いキーを作った: {sorted(unknown)}")
    record = {key: values[key] for key in KEY_ORDER if key in values}
    for key in record.keys() & DERIVED_LABELS.keys():
        # 原文にラベルがあればそちらを優先する。ここは無かったときの控え。
        labels.setdefault(key, DERIVED_LABELS[key])
    return Built(record=record, location=location, labels=labels)


def _split_values(raw: str) -> list[str]:
    """カンマ区切りの 1 欄を配列にする。空要素は落とす (ADR 0012)。

    401 の指定基準は該当しない番号ぶんが空で埋まっており、そのまま配列にすると
    空文字が並ぶ。基準の文言そのものの区切りは読点 (、) なので割られない。

    >>> _split_values("（一）名木、巨樹,,,,二．都城跡、国郡庁跡")
    ['（一）名木、巨樹', '二．都城跡、国郡庁跡']
    >>> _split_values("")
    []
    """
    return [part.strip() for part in raw.split(",") if part.strip()]


def routing_kinds(category: Category, record: dict[str, Any]) -> list[str]:
    """1 件をデータリポジトリへ振り分けるための区分を、レコードから取り出す。

    どのキーを見るかは分類で違い、それはスキーマの知識なのでここに置く
    (``catalog`` は取り出した値だけを受け取る)。区分を持たない分類では空になり、
    受け皿のリポジトリへ行く。

    >>> from heritage_crawler.catalog import DESIGNATED, MONUMENTS, REGISTERED
    >>> routing_kinds(DESIGNATED, {"national_treasure_class": "国宝"})
    ['国宝']
    >>> routing_kinds(MONUMENTS, {"types": ["特別名勝", "特別史跡"]})
    ['特別名勝', '特別史跡']
    >>> routing_kinds(REGISTERED, {"types": ["住宅"]})
    []
    """
    key = ROUTING_KEYS.get(category.code)
    if key is None:
        return []
    value = record.get(key, [])
    return [value] if isinstance(value, str) else list(value)


def squeezed(name: str) -> str:
    """空白を落とした形。台帳と詳細ページの値を比べるときに使う。

    CSV は全角スペース、詳細ページは半角スペースで同じ名称を書くことがある
    (実データ 20,461 件のうち 137 件)。そのまま比べると報告が空白の違いで埋まり、
    **本当の食い違い (結合キーの取り違え) が見えなくなる**。

    >>> squeezed("高照神社　津軽信政公墓 （２）") == squeezed("高照神社 津軽信政公墓 （２）")
    True
    >>> squeezed("秋篠寺本堂") == squeezed("秋篠寺講堂")
    False
    """
    return re.sub(r"\s+", "", name)


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


def _rellists(
    page: DetailPage, category: Category, report: BuildReport, labels: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``detail_rellist_*`` モーダルを附指定と措置に振り分ける (附指定, 措置)。

    分類ではなくラベルで見分ける。同じモーダルが建造物系では附指定の一覧、
    401 では指定等後に行った措置の履歴になっているため。
    """
    annexes: list[dict[str, Any]] = []
    measures: list[dict[str, Any]] = []
    for raw in page.rellists:
        is_measure = bool(raw.keys() & MEASURE_KEYS.keys())
        keys = MEASURE_KEYS if is_measure else ANNEX_KEYS
        target = measures if is_measure else annexes
        prefix = "measures" if is_measure else "annexes"
        if entry := _rellist_entry(raw, keys, category, report, labels, prefix):
            target.append(entry)

    for label, entries in ((ANNEX_LABEL, annexes), (MEASURES_LABEL, measures)):
        if page.related.get(label) and not entries:
            # 「あり」と書いてあるのに一覧が読めていない。取りこぼしを疑う。
            report.missing_rellists[f"{category.code} {label}"] += 1
    return annexes, measures


def _rellist_entry(
    raw: dict[str, str],
    keys: dict[str, Field],
    category: Category,
    report: BuildReport,
    labels: dict[str, str],
    prefix: str,
) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    for label, value in raw.items():
        mapped = keys.get(label)
        if mapped is None:
            report.unknown_labels[f"{category.code} 一覧:{label}"] += 1
            continue
        if not value:
            continue
        if mapped.kind is Kind.LIST:
            entry.setdefault(mapped.key, []).append(value)
        elif mapped.kind is Kind.DATE:
            if (normalized := normalize_date(value)) is None:
                report.invalid_dates[f"{label}: {value}"] += 1
                continue
            entry[mapped.key] = normalized
        else:
            entry[mapped.key] = value
        labels[f"{prefix}.{mapped.key}"] = display_label(label)
    return entry
