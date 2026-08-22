"""catalog モジュールのテスト。

外部サイトへはアクセスしない。URL 形式そのものは 2026-08-11 に実際の
データベースで 200 が返ることを確認済みで、ここではその形を崩さないことを守る。
"""

import re

import pytest

from conftest import fixture
from heritage_crawler import (
    IRREGULAR_AREAS,
    NON_PREFECTURE_AREAS,
    PREFECTURES,
    SEARCH_AREAS,
    SELECTABLE_AREAS,
    TARGET_CATEGORIES,
    detail_url,
)
from heritage_crawler.catalog import (
    DESIGNATED,
    MONUMENTS,
    REGIONS,
    REGISTERED,
    TARGET_DATASETS,
    WHOLE_AREA,
    AreaScope,
    Category,
    areas_for,
    datasets_of,
    search_areas,
)


def test_detail_url_短い連番形式() -> None:
    assert detail_url("102", "23") == "https://kunishitei.bunka.go.jp/heritage/detail/102/23"


def test_detail_url_ゼロ詰め形式のゼロを落とさない() -> None:
    """管理対象ID を数値として扱うとゼロ詰めが落ち、詳細ページに到達できなくなる。"""
    url = detail_url("102", "00003904")
    assert url.endswith("/102/00003904")
    assert "/102/3904" not in url


@pytest.mark.parametrize(
    ("category_code", "kanri_taishou_id"),
    [("", "23"), ("102", ""), ("", "")],
)
def test_detail_url_空の_ID_を拒否する(category_code: str, kanri_taishou_id: str) -> None:
    with pytest.raises(ValueError):
        detail_url(category_code, kanri_taishou_id)


def test_取得対象の分類は_101_102_103_401() -> None:
    """世界遺産 (901) は建造物・記念物と別軸の指定なので含めない (ADR 0002 / 0012)。"""
    assert {c.code for c in TARGET_CATEGORIES} == {"101", "102", "103", "401"}


def test_401の複合指定は両方のリポジトリへ書く() -> None:
    """種別を 2 つ持つ指定は、どちらの種別から見ても構成員 (ADR 0012)。"""
    assert [dataset.repo for dataset in datasets_of(MONUMENTS, ["特別名勝", "特別史跡"])] == [
        "special-historic-sites",
        "special-places-of-scenic-beauty",
    ]


def test_特別指定は通常の種別と排他に振り分ける() -> None:
    """特別史跡は史跡のうちから指定されるが、国宝・重文と同じく排他 (ADR 0012)。"""
    assert [dataset.repo for dataset in datasets_of(MONUMENTS, ["特別史跡"])] == [
        "special-historic-sites"
    ]
    assert [dataset.repo for dataset in datasets_of(MONUMENTS, ["史跡"])] == ["historic-sites"]


def test_区分が読めないときの行き先は分類で違う() -> None:
    """102 には受け皿があるが、401 には無い (種別不明を史跡に紛れ込ませない)。"""
    assert [dataset.repo for dataset in datasets_of(DESIGNATED)] == [
        "important-cultural-properties"
    ]
    assert datasets_of(MONUMENTS) == []


def test_リポジトリ名は重複しない() -> None:
    """出力ディレクトリ配下のディレクトリ名になるため、衝突すると混ざる。"""
    assert len({dataset.repo for dataset in TARGET_DATASETS}) == len(TARGET_DATASETS)


def test_分類コードは文字列で保持する() -> None:
    """CSV の台帳ID と突き合わせるため、数値にせず文字列のまま扱う。"""
    for category in TARGET_CATEGORIES:
        assert isinstance(category.code, str)


def test_選べる地域は_47_都道府県と_2_つの受け皿() -> None:
    """２県以上・地域を定めない を外すと、都道府県で引けない指定を取りこぼす。"""
    assert len(PREFECTURES) == 47
    assert [area.name for area in NON_PREFECTURE_AREAS] == ["２県以上", "地域を定めない"]
    assert len(SELECTABLE_AREAS) == 49


def test_地域の並びと表記が検索フォームの_option_と一致する() -> None:
    """送る値は select の option そのもの。表記が 1 文字でもずれると 0 件になる。"""
    options = re.findall(r'<option value="([^"]*)">', fixture("search_index.html"))
    assert [value for value in options if value] == [area.name for area in SELECTABLE_AREAS]


def test_未正規化の都道府県値も分割軸に含める() -> None:
    """seat_pref は格納値の完全一致。表示名でない値の行は option では引けない。"""
    assert [area.name for area in IRREGULAR_AREAS] == ["98", "1"]
    assert SEARCH_AREAS == SELECTABLE_AREAS + IRREGULAR_AREAS


def test_地域のコードと_slug_は重複しない() -> None:
    """どちらもキャッシュのファイル名になるため、衝突すると上書きが起きる。"""
    assert len({area.code for area in SEARCH_AREAS}) == len(SEARCH_AREAS)
    assert len({area.slug for area in SEARCH_AREAS}) == len(SEARCH_AREAS)


def test_slug_は_ASCII_に限る() -> None:
    """日本語のファイル名は macOS の NFD 正規化で同一性が崩れ、再開判定がずれる。"""
    for area in SEARCH_AREAS:
        assert re.fullmatch(r"[a-z][a-z0-9-]*", area.slug), area


# --- 分類ごとの分割軸 (#74) ---


def test_既定の分類は都道府県で引く() -> None:
    """101 / 102 / 103 / 401 は 47 都道府県 + 受け皿 2 + 未正規化 2。"""
    assert len(search_areas(REGISTERED)) == 51


def test_地域の_9_区分しか持たない分類がある() -> None:
    """無形文化財 (303 / 313) は都道府県では引けない (2026-08-23 実測)。"""
    category = Category("303", "重要無形文化財", 100, area_scope=AreaScope.REGION)

    names = [area.name for area in search_areas(category)]

    assert names == ["全国一円", "東北", "関東", "北陸", "東海", "近畿", "中国", "四国", "九州"]


def test_地域欄の無い分類は全国を_1_回で取る() -> None:
    """選定保存技術 (304) には地域欄そのものが無い。空を送ると全国が返る。"""
    category = Category("304", "選定保存技術", 82, area_scope=AreaScope.WHOLE)

    areas = search_areas(category)

    assert [area.name for area in areas] == [""]
    assert areas[0].slug == "whole"


def test_都道府県と地域の両方を持つ分類がある() -> None:
    """無形民俗文化財 (302 / 322 / 312 / 323) は両方の option を持つ。"""
    category = Category(
        "302", "重要無形民俗文化財", 338, area_scope=AreaScope.PREFECTURE_AND_REGION
    )

    areas = search_areas(category)

    assert len(areas) == 51 + 9
    assert "全国一円" in [area.name for area in areas]


def test_地域を明示すればそちらが優先される() -> None:
    """``--area`` で絞ったときは分割軸より指定が勝つ。"""
    only_tokyo = [area for area in PREFECTURES if area.name == "東京都"]

    assert list(areas_for(REGISTERED, only_tokyo)) == only_tokyo
    assert areas_for(REGISTERED, None) == search_areas(REGISTERED)


def test_地域コードは分割軸をまたいでも重ならない() -> None:
    """コードはキャッシュのファイル名になる。重なると別の地域の CSV を上書きする。"""
    every = PREFECTURES + NON_PREFECTURE_AREAS + IRREGULAR_AREAS + REGIONS + (WHOLE_AREA,)

    codes = [area.code for area in every]
    slugs = [area.slug for area in every]

    assert len(set(codes)) == len(codes)
    assert len(set(slugs)) == len(slugs)
