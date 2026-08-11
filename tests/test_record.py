"""出力レコードの組み立てのテスト (ADR 0008 の決定がそのまま検査項目)。

**外部サイトへは出ない。** 詳細ページは ``DetailPage`` を直に組み立てて渡す
(HTML の読み取りは ``test_detail_page`` の担当)。
"""

from __future__ import annotations

import pytest

from conftest import make_row
from heritage_crawler.catalog import BUILDING_CATEGORIES
from heritage_crawler.detail_page import DetailPage
from heritage_crawler.ledger import LedgerRow
from heritage_crawler.record import (
    KEY_ORDER,
    BuildReport,
    build_record,
    normalize_date,
    resolve_location,
)

REGISTERED, DESIGNATED, SELECTED = BUILDING_CATEGORIES  # 101 / 102 / 103


def row(category=DESIGNATED, **columns: str) -> LedgerRow:  # type: ignore[no-untyped-def]
    values = {"台帳ID": category.code, "管理対象ID": "2485"} | columns
    return LedgerRow(category=category, values=tuple(make_row(values)))


def page(fields: dict[str, str] | None = None) -> DetailPage:
    """詳細ページの項目。ラベルは辞書で渡す。

    キーワード引数にはできない — Python は識別子を NFKC で正規化するので、
    ``種別１`` (全角) が ``種別1`` (半角) に化けて実データのラベルと合わなくなる。
    """
    return DetailPage(fields={"名称": "秋篠寺本堂", "所在都道府県": "奈良県"} | (fields or {}))


def build(row_: LedgerRow, page_: DetailPage) -> tuple[dict, BuildReport]:  # type: ignore[type-arg]
    report = BuildReport()
    return build_record(row_, page_, report).record, report


def test_原文のラベルを正規化したキーへ移す() -> None:
    record, report = build(
        row(), page({"ふりがな": "あきしのでらほんどう", "構造及び形式等": "桁行五間"})
    )

    assert record["ledger_id"] == "102"
    assert record["managed_id"] == "2485"
    assert record["name"] == "秋篠寺本堂"
    assert record["name_kana"] == "あきしのでらほんどう"
    assert record["structure"] == "桁行五間"
    assert record["url"] == "https://kunishitei.bunka.go.jp/heritage/detail/102/2485"
    assert not report.unknown_labels


def test_分類ごとに名前の違う項目を同じキーへ寄せる() -> None:
    """指定 (102) / 登録 (101) / 選定 (103) を 1 つのスキーマで読めるようにする。"""
    designated, _ = build(
        row(), page({"指定番号": "00153", "重文指定年月日": "1898.12.28(明治31.12.28)"})
    )
    registered, _ = build(
        row(REGISTERED), page({"登録番号": "13 － 0180", "登録年月日": "2004.11.08(平成16.11.08)"})
    )
    selected, _ = build(
        row(SELECTED), page({"告示番号": "12", "選定年月日": "1988.12.16(昭和63.12.16)"})
    )

    assert (designated["designation_kind"], designated["designated_date"]) == (
        "指定",
        "1898-12-28",
    )
    assert (registered["designation_kind"], registered["designated_date"]) == ("登録", "2004-11-08")
    assert (selected["designation_kind"], selected["designated_date"]) == ("選定", "1988-12-16")
    assert registered["designation_number"] == "13 － 0180"


def test_種別と基準は配列にまとめる() -> None:
    record, _ = build(
        row(REGISTERED),
        page({"種別１": "住宅", "種別２": "建築物", "登録基準１": "造形の規範となっているもの"}),
    )

    assert record["types"] == ["住宅", "建築物"]
    assert record["criteria"] == ["造形の規範となっているもの"]


def test_空の項目はキーごと落とす() -> None:
    """``null`` も空文字も出さない (ADR 0008 の 6)。"""
    record, _ = build(row(), page())

    assert "ridge_name" not in record
    assert "latitude" not in record
    assert "description" not in record


def test_キーの並びは固定する() -> None:
    """同じ内容なら同じ行になること。並びが揺れると差分が読めなくなる。"""
    record, _ = build(row(緯度="34.7", 経度="135.7"), page({"所在地": "奈良県奈良市秋篠町"}))

    assert list(record) == [key for key in KEY_ORDER if key in record]


def test_緯度経度は_CSV_から採る() -> None:
    """詳細ページにしか無い項目と、CSV にしか無い項目を統合する (ADR 0002)。"""
    record, _ = build(row(緯度="34.70361000000000", 経度="135.77621013000000"), page())

    assert (record["latitude"], record["longitude"]) == (34.70361, 135.77621013)


def test_CSV_の日付や種別は使わない() -> None:
    """CSV の列は分類によって意味が変わる (101 の重文指定年月日は登録年月日)。"""
    record, _ = build(row(REGISTERED, 重文指定年月日="20041108", 種別1="住宅"), page())

    assert "designated_date" not in record
    assert "types" not in record


def test_附指定は配列にし添付ファイルは有無だけ持つ() -> None:
    record, report = build(
        row(),
        DetailPage(
            fields={"名称": "石上神宮拝殿", "所在都道府県": "奈良県"},
            related={"附指定": True, "添付ファイル": False},
            annexes=({"附名称": "棟札", "附員数": "6枚"},),
            has_photo=True,
        ),
    )

    assert record["annexes"] == [{"name": "棟札", "quantity": "6枚"}]
    assert record["has_attachment"] is False
    assert record["has_photo"] is True
    assert report.missing_annexes == 0


def test_附指定ありなのに一覧が空なら報せる() -> None:
    _, report = build(
        row(),
        DetailPage(fields={"名称": "石上神宮拝殿"}, related={"附指定": True}),
    )

    assert report.missing_annexes == 1


def test_対応表に無いラベルは捨てずに数える() -> None:
    """サイト側に項目が増えたことに気付ける唯一の場所 (ADR 0008 の 8)。"""
    record, report = build(row(), page({"新項目": "なにか", "関連情報": "x"}))

    assert "新項目" not in str(record)
    assert report.unknown_labels["102 新項目"] == 1
    assert report.has_anomalies


def test_日付として読めない値は入れずに数える() -> None:
    _, report = build(row(), page({"重文指定年月日": "明治31年ごろ"}))

    assert report.invalid_dates["重文指定年月日: 明治31年ごろ"] == 1


def test_CSV_と詳細で名称が違えば報せる() -> None:
    """結合キーの取り違えは、まず名称のずれとして現れる。"""
    _, report = build(row(名称="別の建物"), page())

    assert report.name_mismatches == ["102/2485 CSV=別の建物 / 詳細=秋篠寺本堂"]


def test_棟名まで含めて名称を突き合わせる() -> None:
    _, report = build(
        row(名称="琵琶湖疏水施設", 棟名="第一トンネル"),
        page({"名称": "琵琶湖疏水施設", "棟名": "第一トンネル"}),
    )

    assert report.name_mismatches == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2004.11.08(平成16.11.08)", "2004-11-08"),
        ("1898.12.28(明治31.12.28)", "1898-12-28"),
        ("1976.9.4", "1976-09-04"),
        ("1976.09", "1976-09"),
        ("1976", "1976"),
        ("平成16年", None),
        ("", None),
    ],
)
def test_日付は_ISO_8601_に正規化する(raw: str, expected: str | None) -> None:
    assert normalize_date(raw) == expected


def test_所在都道府県が都道府県名ならそれを使う() -> None:
    location = resolve_location("奈良県", "奈良県奈良市秋篠町")

    assert (location.prefecture, location.area.code, location.from_address) == (
        "奈良県",
        "29",
        False,
    )


def test_都道府県が未正規化なら所在地から拾い直す() -> None:
    """101 に 3 件ある ``98`` / ``1`` の行 (catalog.IRREGULAR_AREAS)。"""
    location = resolve_location("1", "神奈川県横浜市磯子区森二丁目481")

    assert (location.prefecture, location.area.slug, location.from_address) == (
        "神奈川県",
        "kanagawa",
        True,
    )


def test_所在地に複数の都道府県が出るなら２県以上にする() -> None:
    location = resolve_location("98", "山梨県北杜市白州町／長野県諏訪郡富士見町落合")

    assert (location.prefecture, location.area.code) == ("", "90")


def test_都道府県が決まらなければ地域を定めないへ送る() -> None:
    location = resolve_location("", "")

    assert (location.prefecture, location.area.code) == ("", "99")


def test_都道府県は決まった経緯まで数える() -> None:
    """所在地から拾った件数は、元データが直れば 0 になる指標 (ADR 0008 の 4)。"""
    record, report = build(row(SELECTED), DetailPage(fields={"所在地": "京都府京都市"}))

    assert record["prefecture"] == "京都府"
    assert (report.prefecture_from_address, report.prefecture_unresolved) == (1, 0)


def test_都道府県が決まらなければキーを出さない() -> None:
    record, report = build(
        row(), page({"所在都道府県": "98", "所在地": "山梨県北杜市／長野県諏訪郡"})
    )

    assert "prefecture" not in record
    assert report.prefecture_unresolved == 1
