"""catalog モジュールのテスト。

外部サイトへはアクセスしない。URL 形式そのものは 2026-08-11 に実際の
データベースで 200 が返ることを確認済みで、ここではその形を崩さないことを守る。
"""

import pytest

from heritage_crawler import BUILDING_CATEGORIES, detail_url


def test_detail_url_短い連番形式() -> None:
    assert detail_url("102", "23") == "https://kunishitei.bunka.go.jp/heritage/detail/102/23"


def test_detail_url_ゼロ詰め形式のゼロを落とさない() -> None:
    """管理対象ID を数値として扱うとゼロ詰めが落ち、詳細ページに到達できなくなる。"""
    url = detail_url("102", "00003904")
    assert url.endswith("/102/00003904")
    assert "/102/3904" not in url


@pytest.mark.parametrize(
    ("daichou_id", "kanri_taishou_id"),
    [("", "23"), ("102", ""), ("", "")],
)
def test_detail_url_空の_ID_を拒否する(daichou_id: str, kanri_taishou_id: str) -> None:
    with pytest.raises(ValueError):
        detail_url(daichou_id, kanri_taishou_id)


def test_建造物系の分類は_101_102_103() -> None:
    """世界遺産 (901) は建造物と別軸の指定なので含めない (ADR 0002)。"""
    assert {c.code for c in BUILDING_CATEGORIES} == {"101", "102", "103"}


def test_分類コードは文字列で保持する() -> None:
    """CSV の台帳ID と突き合わせるため、数値にせず文字列のまま扱う。"""
    for category in BUILDING_CATEGORIES:
        assert isinstance(category.code, str)
