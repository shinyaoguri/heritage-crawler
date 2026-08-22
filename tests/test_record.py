"""出力レコードの組み立てのテスト (ADR 0008 の決定がそのまま検査項目)。

**外部サイトへは出ない。** 詳細ページは ``DetailPage`` を直に組み立てて渡す
(HTML の読み取りは ``test_detail_page`` の担当)。
"""

from __future__ import annotations

import pytest

from conftest import make_row
from heritage_crawler.catalog import (
    CONSERVATION_TECHNIQUES,
    DESIGNATED,
    DOCUMENTED_INTANGIBLE,
    DOCUMENTED_INTANGIBLE_FOLK,
    FINE_ARTS,
    INTANGIBLE,
    INTANGIBLE_FOLK,
    MONUMENTS,
    REGISTERED,
    REGISTERED_MONUMENTS,
    SELECTED,
    TANGIBLE_FOLK,
    WORLD_HERITAGE,
)
from heritage_crawler.detail_page import DetailPage
from heritage_crawler.ledger import LedgerRow
from heritage_crawler.record import (
    KEY_ORDER,
    BuildReport,
    build_record,
    category_of,
    normalize_date,
    resolve_location,
    routing_kinds,
)

# 101 / 102 / 103 / 401。分類は 19 個あるので、位置ではなく名前で引く。


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


# --- 由来の分類 (ADR 0024) ---


def test_レコードは由来の分類コードを持つ() -> None:
    """台帳ID からは分類を戻せない。1 行が自分で名乗る (#74)。"""
    built = build_record(row(MONUMENTS), page(), BuildReport())
    assert built.record["category_code"] == "401"
    assert built.record["category_name"] == "史跡名勝天然記念物"


def test_分類は台帳ID_ではなく分類コードから戻す() -> None:
    """台帳ID 401 には 401 / 411 / 412 が同居する (#74)。

    台帳ID を見ていたら、登録記念物の行を史跡名勝天然記念物として扱ってしまう。
    """
    record = {"ledger_id": "401", "managed_id": "1", "category_code": "103"}
    assert category_of(record) is SELECTED


def test_分類コードを持たない行は台帳ID_から戻す() -> None:
    """2026-08-23 より前に書いた行への控え (ADR 0024)。

    当時の 4 分類では台帳ID と分類コードが同値だったので、これで正しい。
    """
    assert category_of({"ledger_id": "102", "managed_id": "23"}) is DESIGNATED


def test_どちらからも戻せない行は名指しで断る() -> None:
    with pytest.raises(KeyError):
        category_of({"ledger_id": "999", "managed_id": "1"})


# --- 15 分類ぶんの項目 (ADR 0025) ---


def test_美術工芸品の項目を読む() -> None:
    """201 / 211 は建造物に無い項目を持つ (2026-08-23 実測)。"""
    built = build_record(
        row(FINE_ARTS),
        page(
            {
                "国": "日本",
                "作者": "伝狩野宗秀",
                "ト書": "各巻末に文禄三年七月最上義光寄進の記がある",
                "枝番": "00",
                "指定番号（登録番号）": "01893",
                "国宝・重文区分": "重要文化財",
            }
        ),
        BuildReport(),
    )

    assert built.record["country"] == "日本"
    assert built.record["author"] == "伝狩野宗秀"
    assert built.record["branch_number"] == "00"
    assert built.record["designation_number"] == "01893"
    assert "各巻末に" in built.record["annotation"]


def test_美術工芸品も国宝と重要文化財に振り分ける() -> None:
    """102 と同じ ``国宝・重文区分`` で決まる (ADR 0025)。"""
    assert routing_kinds(FINE_ARTS, {"national_treasure_class": "国宝"}) == ["国宝"]


def test_選択と認定は指定とは別の行為() -> None:
    """312 / 313 は選択、303 / 323 / 304 は認定 (ADR 0025)。"""
    kinds = {
        code: build_record(
            row(category), page(), BuildReport()
        ).record["designation_kind"]
        for code, category in (
            ("312", DOCUMENTED_INTANGIBLE_FOLK),
            ("303", INTANGIBLE),
            ("304", CONSERVATION_TECHNIQUES),
            ("411", REGISTERED_MONUMENTS),
        )
    }

    assert kinds == {"312": "選択", "303": "認定", "304": "認定", "411": "登録"}


def test_登録記念物の基準はカンマで割る() -> None:
    """401 の ``指定基準`` と同じ形式 (1 欄にカンマ区切り)。"""
    built = build_record(
        row(REGISTERED_MONUMENTS),
        page({"登録基準": "一 造園文化の発展に寄与しているもの,,三 歴史的意義を有するもの"}),
        BuildReport(),
    )

    assert built.record["criteria"] == [
        "一 造園文化の発展に寄与しているもの",
        "三 歴史的意義を有するもの",
    ]


def test_無形民俗文化財の所在は地域のこともある() -> None:
    """欄の名前が ``所在都道府県、地域`` になる (302 / 322 / 312)。"""
    built = build_record(
        row(INTANGIBLE_FOLK),
        page({"所在都道府県、地域": "京都府", "保護団体名": "祇園祭山鉾連合会"}),
        BuildReport(),
    )

    assert built.record["prefecture"] == "京都府"
    assert built.record["protection_organization"] == "祇園祭山鉾連合会"


def test_世界遺産の構成資産を残す() -> None:
    built = build_record(
        row(WORLD_HERITAGE),
        page({"構成資産": "中尊寺、毛越寺、観自在王院跡", "登録基準６": "平泉の浄土庭園は…"}),
        BuildReport(),
    )

    assert built.record["component_assets"] == "中尊寺、毛越寺、観自在王院跡"
    assert built.record["criteria"] == ["平泉の浄土庭園は…"]


# --- 関連情報モーダル (ADR 0025) ---


def holders_page(*entries: dict[str, str], related: str = "団体情報") -> DetailPage:
    """保持者・保持団体の一覧を持つ詳細ページ。"""
    return DetailPage(
        fields={"名称": "伊勢型紙"},
        related={related: True},
        rellists=tuple(entries),
    )


def test_保持団体の一覧を読む() -> None:
    """無形文化財では主情報に保持者が出てこない。ここが中核のデータになる。"""
    built = build_record(
        row(INTANGIBLE),
        holders_page(
            {
                "団体情報の名称": "伊勢型紙技術保存会",
                "団体情報の名称 ふりがな": "いせかたがみぎじゅつほぞんかい",
                "団体情報の代表者氏名": "内田勲",
                "認定・指定年月日": "1993.04.15(平成5.04.15)",
            }
        ),
        BuildReport(),
    )

    assert built.record["holders"] == [
        {
            "kind": "保持団体",
            "name": "伊勢型紙技術保存会",
            "name_kana": "いせかたがみぎじゅつほぞんかい",
            "representative": "内田勲",
            "date": "1993-04-15",
        }
    ]


def test_保持者と保持団体は欄の名前で見分ける() -> None:
    """同じ ``name`` へ寄せても、個人か団体かは失われない。"""
    built = build_record(
        row(DOCUMENTED_INTANGIBLE),
        holders_page(
            {
                "保持者（関係技芸者）の氏名": "高坂水雄",
                "保持者（関係技芸者）の芸名・雅号等": "高坂雄水",
            },
            related="保持者情報（保持者／芸名・雅号）",
        ),
        BuildReport(),
    )

    assert built.record["holders"] == [
        {"kind": "保持者", "name": "高坂水雄", "alias": "高坂雄水"}
    ]


def test_選定保存技術は認定を指名と書く() -> None:
    """304 だけ ``認定・指名年月日``。同じ ``date`` へ寄せる。"""
    built = build_record(
        row(CONSERVATION_TECHNIQUES),
        holders_page(
            {
                "保持団体（関係技芸者の団体）の名称": "阿波藍製造技術保存会",
                "認定・指名年月日": "1978.05.09(昭和53.05.09)",
            },
            related="保持団体（関係技芸者の団体）",
        ),
        BuildReport(),
    )

    assert built.record["holders"][0]["date"] == "1978-05-09"


def test_民俗文化財の附は附指定へ寄せる() -> None:
    """301 / 311 では見出しが ``附`` と短い。附指定と同じもの。"""
    report = BuildReport()
    build_record(
        row(TANGIBLE_FOLK),
        DetailPage(fields={"名称": "アイヌの生活用具"}, related={"附": True}),
        report,
    )

    assert not report.unknown_labels
    # 「あり」なのに一覧が空なので、取りこぼしとして報告に出る
    assert dict(report.missing_rellists) == {"301 附": 1}


def test_美術工芸品の一つ書は有無だけ残す() -> None:
    """一覧そのものが空なので、取りこぼしとしては数えない (実測)。"""
    report = BuildReport()
    built = build_record(
        row(FINE_ARTS),
        DetailPage(fields={"名称": "紙本著色遊行上人絵"}, related={"一つ書": True}),
        report,
    )

    assert built.record["has_itemization"] is True
    assert not report.unknown_labels
    assert not report.missing_rellists


def test_世界遺産の関連情報は他分類へのリンク() -> None:
    """構成資産が別の分類として現れる (ADR 0025)。分類名を未知として数えない。"""
    report = BuildReport()
    built = build_record(
        row(WORLD_HERITAGE),
        DetailPage(
            fields={"名称": "平泉"},
            related={"国宝・重要文化財（建造物）": True, "史跡名勝天然記念物": True},
        ),
        report,
    )

    assert built.record["has_related_properties"] is True
    assert not report.unknown_labels
