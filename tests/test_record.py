"""出力レコードの組み立てのテスト (ADR 0008 の決定がそのまま検査項目)。

**外部サイトへは出ない。** 詳細ページは ``DetailPage`` を直に組み立てて渡す
(HTML の読み取りは ``test_detail_page`` の担当)。
"""

from __future__ import annotations

import pytest

from conftest import make_row
from heritage_crawler.catalog import TARGET_CATEGORIES
from heritage_crawler.detail_page import DetailPage
from heritage_crawler.ledger import LedgerRow
from heritage_crawler.record import (
    KEY_ORDER,
    BuildReport,
    build_record,
    normalize_date,
    resolve_location,
    routing_kinds,
)

REGISTERED, DESIGNATED, SELECTED, MONUMENTS = TARGET_CATEGORIES  # 101 / 102 / 103 / 401


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


def test_名称の食い違いは空白の違いを無視して見る() -> None:
    """CSV は全角スペース、詳細は半角スペースのことがある (実データ 137 件)。

    空白の違いで報告が埋まると、本当の食い違い (結合キーの取り違え) が見えなくなる。
    """
    _, same = build(
        row(名称="高照神社", 棟名="津軽信政公墓　（２）"),
        page({"名称": "高照神社", "棟名": "津軽信政公墓 （２）"}),
    )
    assert same.name_mismatches == []

    _, different = build(row(名称="秋篠寺本堂"), page({"名称": "秋篠寺講堂"}))
    assert len(different.name_mismatches) == 1


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
            rellists=({"附名称": "棟札", "附員数": "6枚"},),
            has_photo=True,
        ),
    )

    assert record["annexes"] == [{"name": "棟札", "quantity": "6枚"}]
    assert record["has_attachment"] is False
    assert record["has_photo"] is True
    assert not report.missing_rellists


def test_附指定ありなのに一覧が空なら報せる() -> None:
    _, report = build(
        row(),
        DetailPage(fields={"名称": "石上神宮拝殿"}, related={"附指定": True}),
    )

    assert report.missing_rellists["102 附指定"] == 1
    assert report.has_anomalies


def test_401の同じモーダルは附指定ではなく措置の履歴として読む() -> None:
    """``detail_rellist_*`` の中身は分類で変わる (ADR 0012)。

    ラベルで見分けないと、指定の変遷が附指定として読まれるか捨てられる。
    """
    record, report = build(
        row(MONUMENTS, 名称="旧浜離宮庭園"),
        DetailPage(
            fields={"名称": "旧浜離宮庭園", "所在都道府県": "東京都"},
            related={"指定等後に行った措置": True, "添付ファイル": False},
            rellists=(
                {
                    "異動年月日": "1952.11.22(昭和27.11.22)",
                    "異動種別1": "特別名勝",
                    "異動種別2": "特別史跡",
                    "異動種別3": "",
                    "異動内容": "",
                },
            ),
        ),
    )

    assert record["measures"] == [{"date": "1952-11-22", "types": ["特別名勝", "特別史跡"]}]
    assert "annexes" not in record
    assert record["has_measures"] is True
    assert not report.missing_rellists
    assert not report.unknown_labels


def test_401の措置ありなのに一覧が空なら報せる() -> None:
    _, report = build(
        row(MONUMENTS),
        DetailPage(fields={"名称": "旧浜離宮庭園"}, related={"指定等後に行った措置": True}),
    )

    assert report.missing_rellists["401 指定等後に行った措置"] == 1


def test_401の指定基準はカンマで割って空を落とす() -> None:
    """建造物系は基準を欄で分けるが、401 は 1 欄に並べる (ADR 0012)。"""
    record, _ = build(
        row(MONUMENTS),
        DetailPage(fields={"名称": "浦富海岸", "指定基準": "五．岩石、洞穴,,（一）岩石、鉱物"}),
    )

    assert record["criteria"] == ["五．岩石、洞穴", "（一）岩石、鉱物"]


def test_401の種別と特別指定を読む() -> None:
    record, report = build(
        row(MONUMENTS),
        DetailPage(
            fields={
                "名称": "阿寒湖のマリモ",
                "種別１": "特別天然記念物",
                "特別区分": "特別",
                "指定年月日": "1921.03.03(大正10.03.03)",
                "特別指定年月日": "1952.03.29(昭和27.03.29)",
                "所在地（市区町村）": "北海道釧路市阿寒町",
            }
        ),
    )

    assert record["types"] == ["特別天然記念物"]
    assert record["special_class"] == "特別"
    assert record["designation_kind"] == "指定"
    assert record["designated_date"] == "1921-03-03"
    assert record["special_designated_date"] == "1952-03-29"
    assert record["address"] == "北海道釧路市阿寒町"
    assert not report.unknown_labels


def test_振り分けの区分は分類ごとに違うキーから採る() -> None:
    """102 は国宝・重文区分、401 は種別 (ADR 0012)。"""
    assert routing_kinds(DESIGNATED, {"national_treasure_class": "国宝"}) == ["国宝"]
    assert routing_kinds(MONUMENTS, {"types": ["名勝", "天然記念物"]}) == ["名勝", "天然記念物"]
    # 区分を持たない分類は空 (受け皿のリポジトリ 1 つへ行く)
    assert routing_kinds(REGISTERED, {"types": ["住宅"]}) == []
    assert routing_kinds(MONUMENTS, {}) == []


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


def test_url_は_CSV_の台帳ID_列ではなく分類コードで組む() -> None:
    """詳細ページ URL の第 1 セグメントは分類コード (#74)。

    現行 4 分類は台帳ID と分類コードが同値だが、411 (登録記念物) の台帳ID は
    401 で、台帳ID で組むとエラーページが返る。``ledger_id`` は CSV の値のまま残す。
    """
    built = build_record(row(MONUMENTS, **{"台帳ID": "999"}), page(), BuildReport())
    assert built.record["url"] == "https://kunishitei.bunka.go.jp/heritage/detail/401/2485"
    assert built.record["ledger_id"] == "999"
