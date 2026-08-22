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
from heritage_crawler.catalog import DESIGNATED, MONUMENTS, TARGET_DATASETS, datasets_of


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
