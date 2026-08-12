"""詳細ページの読み取りのテスト。

**外部サイトへは出ない。** 3 分類ぶんの実応答を切り出した
``tests/fixtures/detail_10*.html`` だけを読む。ここで守りたいのは
「分類によって項目が違っても、原文をそのまま拾えること」。
"""

from __future__ import annotations

import pytest

from conftest import fixture
from heritage_crawler.detail_page import ParseError, parse_detail_page


def test_101_の主情報を原文のラベルのまま読む() -> None:
    page = parse_detail_page(fixture("detail_101.html"))

    assert page.fields["名称"] == "青柳家住宅主屋"
    assert page.fields["ふりがな"] == "あおやぎけじゅうたくしゅおく"
    assert page.fields["種別１"] == "住宅"
    assert page.fields["種別２"] == "建築物"
    assert page.fields["登録番号"] == "13 － 0180"
    assert page.fields["登録年月日"] == "2004.11.08(平成16.11.08)"
    assert page.fields["所在都道府県"] == "東京都"


def test_値が空の項目は落とす() -> None:
    """欄はあるが空、を「値がある」と扱うと欠損の判定がずれる。"""
    page = parse_detail_page(fixture("detail_101.html"))

    assert "追加年月日" not in page.fields
    assert "所有者名" not in page.fields


def test_解説文は数値文字参照の見出しでも読める() -> None:
    """本文側の見出しは ``&#35299;&#35500;&#25991;`` と書かれている。"""
    page = parse_detail_page(fixture("detail_101.html"))

    assert page.description.startswith("道路に北面する敷地の中央に建つ。")
    assert page.detailed_description == ""


def test_102_は詳細解説と附指定を持つ() -> None:
    page = parse_detail_page(fixture("detail_102.html"))

    assert page.fields["国宝・重文区分"] == "国宝"
    assert page.fields["種別"] == "近世以前／神社"
    assert page.detailed_description.startswith("石上神宮拝殿　一棟")
    assert "\n" in page.detailed_description  # textarea の改行は保つ
    assert page.rellists == ({"附名称": "棟札", "附員数": "6枚"},)
    assert page.related == {"附指定": True, "添付ファイル": False}
    assert page.has_photo is False


def test_103_には所在都道府県が無く面積がある() -> None:
    """項目の集合が分類ごとに違う (ADR 0008 の文脈)。"""
    page = parse_detail_page(fixture("detail_103.html"))

    assert "所在都道府県" not in page.fields
    assert "員数" not in page.fields
    assert page.fields["面積"] == "2.7 ha"
    assert page.fields["所在地"] == "京都府京都市"
    assert page.fields["選定基準１"].startswith("（三）")
    assert page.has_photo is True


def test_入れ子の表があっても行が混ざらない() -> None:
    """主情報の表は外側の表の中に入っている。値に表全体の文字列が入らないこと。"""
    page = parse_detail_page(fixture("detail_102.html"))

    assert page.fields["所在地"] == "奈良県天理市布留町"
    assert all(len(value) < 200 for value in page.fields.values())


def test_項目が無いページは弾く() -> None:
    """エラーページを空のレコードとして黙って通さない。"""
    with pytest.raises(ParseError, match="主情報の項目が 1 つも無い"):
        parse_detail_page("<html><body><p>ただいま混み合っています</p></body></html>")
