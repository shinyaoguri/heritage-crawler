"""検索応答の読み取りのテスト。

フィクスチャは 2026-08-11 の実応答から切り出したもの。外部サイトへは出ない。
"""

import pytest

from conftest import fixture
from heritage_crawler.search_page import ParseError, extract_csrf_token, parse_search_page


def test_トップページから_CSRF_トークンを取り出す() -> None:
    assert extract_csrf_token(fixture("search_index.html")) == "d" * 128


def test_CSRF_トークンが無ければ失敗させる() -> None:
    with pytest.raises(ParseError, match="_csrfToken"):
        extract_csrf_token("<html><body><form></form></body></html>")


def test_件数は指定単位の総数を読む() -> None:
    """102 × 北海道 は 34 件 (指定)。CSV の 85 行 (棟) とは単位が違う。"""
    assert parse_search_page(fixture("search_hit.html")).hit_count == 34


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1件中 1件から1件のデータです。", 1), ("14,748件中 1件から20件のデータです。", 14748)],
)
def test_桁区切りの入った件数も読める(text: str, expected: int) -> None:
    html = (
        f'<table><tr><td class="searchnum">{text}</td></tr>'
        '<form action="/utile/csv-list"></form></table>'
    )
    assert parse_search_page(html).hit_count == expected


def test_CSV_フォームの_hidden_値をそのままの並びで返す() -> None:
    """推測して組み立てると 504 になるため、HTML から取った値をそのまま使う (ADR 0002)。"""
    page = parse_search_page(fixture("search_hit.html"))
    assert page.csv_fields == (
        ("_method", "POST"),
        ("_csrfToken", "d" * 128),
        ("screen_id", "index"),
        ("page_no", "1"),
        ("register_sub_id", "102"),
        ("seat_pref", "北海道"),
    )


def test_hidden_以外の入力は混ぜない() -> None:
    """送信ボタンや検索欄まで送ると、送信値が実際のフォームとずれる。"""
    html = (
        '<table><tr><td class="searchnum">5件中 1件から5件のデータです。</td></tr>'
        '<form action="/utile/csv-list">'
        '<input type="hidden" name="page_no" value="1"/>'
        '<input type="text" name="keyword" value="寺"/>'
        '<input type="checkbox" name="only_photo" value="1"/>'
        '<input type="submit" name="csv" value="CSV出力"/>'
        "</form></table>"
    )
    assert parse_search_page(html).csv_fields == (("page_no", "1"),)


def test_0_件のときは_CSV_フォームが無いので_None() -> None:
    page = parse_search_page(fixture("search_empty.html"))
    assert page.hit_count == 0
    assert page.csv_fields is None


def test_件数表示が無ければ失敗させる() -> None:
    """検索が成立していない (エラーページなど) のを、空振りとして通さない。"""
    with pytest.raises(ParseError, match="searchnum"):
        parse_search_page("<html><body>メンテナンス中</body></html>")


def test_件数があるのに_CSV_フォームが無ければ失敗させる() -> None:
    html = '<table><tr><td class="searchnum">5件中 1件から5件のデータです。</td></tr></table>'
    with pytest.raises(ParseError, match="csv-list"):
        parse_search_page(html)
