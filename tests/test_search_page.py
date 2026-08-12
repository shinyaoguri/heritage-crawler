"""検索応答の読み取りのテスト。

フィクスチャは 2026-08-11 の実応答から切り出したもの。外部サイトへは出ない。
"""

import pytest

from conftest import fixture
from heritage_crawler.search_page import (
    ParseError,
    extract_csrf_token,
    parse_listing_page,
    parse_search_page,
)


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


def test_一覧の行からキーと名称を読む() -> None:
    """名称は行に 2 つあるリンク (矢印画像と名称) のうち、文字のある方から採る。"""
    rows = parse_listing_page(fixture("listing_p1.html")).rows
    assert [(row.key, row.name) for row in rows] == [
        ("401/2", "阿寒湖のマリモ"),
        ("401/3420", "山陰道 蒲生峠越"),
        ("401/00003542", "智頭往来 志戸坂峠越"),
    ]


def test_管理対象IDのゼロ詰めを落とさない() -> None:
    """数値にすると 00003542 が 3542 になり、詳細ページへ到達できなくなる。"""
    rows = parse_listing_page(fixture("listing_p1.html")).rows
    assert rows[2].kanri_taishou_id == "00003542"


def test_地域欄は見出しの位置から引く() -> None:
    """見出しに colspan があるので、何セル目かではなく何列目かで引く。

    3 行目は実在する取りこぼしで、**地域欄が空**。これが空だと seat_pref の
    どの値でも引けない (Issue #28)。
    """
    rows = parse_listing_page(fixture("listing_p1.html")).rows
    assert [row.area for row in rows] == ["北海道", "２県以上", ""]


def test_地図表示ボタンから緯度経度を読む() -> None:
    """CSV が下流へ渡しているのは緯度経度だけなので、ここが回収の要になる。"""
    rows = parse_listing_page(fixture("listing_p1.html")).rows
    assert (rows[0].latitude, rows[0].longitude) == (
        "43.45322226000000",
        "144.10229501000000",
    )


def test_地図表示の無い行は緯度経度が空() -> None:
    rows = parse_listing_page(fixture("listing_p2.html")).rows
    assert (rows[1].latitude, rows[1].longitude) == ("", "")


def test_ページャの_hidden_値をページ番号で引ける() -> None:
    """ページ送りも推測して組み立てない。番号は page_no ではなく pageNumber。"""
    page = parse_listing_page(fixture("listing_p1.html"))
    assert dict(page.fields_for_page(2) or ()) == {
        "_method": "POST",
        "_csrfToken": "dddd",
        "screen_id": "index",
        "page_no": "1",
        "register_sub_id": "401",
        "sortTarget": "area",
        "sortType": "asc",
        "pageNumber": "2",
    }


def test_最終ページには次のページが無い() -> None:
    """ここでページ送りが自然に止まる。"""
    assert parse_listing_page(fixture("listing_p2.html")).fields_for_page(2) is None


def test_表示件数フォームは_select_で見分ける() -> None:
    """並べ替えフォームは hidden の pageSize を持つので、名前だけでは混ざる。"""
    page = parse_listing_page(fixture("listing_p2.html"))
    assert dict(page.page_size_fields or ())["sortTarget"] == "area"


def test_件数があるのに一覧の行が無ければ失敗させる() -> None:
    html = '<table><tr><td class="searchnum">5件中 1件から5件のデータです。</td></tr></table>'
    with pytest.raises(ParseError, match="一覧の行"):
        parse_listing_page(html)


def test_0_件のときは行が無くても通る() -> None:
    page = parse_listing_page(fixture("search_empty.html"))
    assert (page.hit_count, page.rows) == (0, ())
