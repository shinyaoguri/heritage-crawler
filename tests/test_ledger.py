"""台帳取得層のテスト。

FakeFetcher を挟んでいるので外部サイトへは出ない (CLAUDE.md)。
応答 HTML は 2026-08-11 の実物から切り出したフィクスチャ。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import FakeFetcher, fixture, make_csv
from heritage_crawler.cache import LedgerCache
from heritage_crawler.catalog import BUILDING_CATEGORIES, SEARCH_AREAS
from heritage_crawler.ledger import (
    CSV_URL,
    INDEX_URL,
    SEARCH_URL,
    LedgerError,
    fetch_ledgers,
    format_summary,
    read_csv_rows,
    search_fields,
    summarize,
)
from heritage_crawler.search_page import ParseError

CATEGORY = BUILDING_CATEGORIES[1]  # 102
HOKKAIDO = SEARCH_AREAS[0]
TOKYO = SEARCH_AREAS[12]

SAMPLE_ROW = [
    "102", "23", "旧旭川偕行社", "", "国宝・重要文化財（建造物）", "重要文化財",
    "近代／文化施設", "", "明治", "19890519", "", "北海道", "北海道旭川市", "",
    "旭川市", "", "43.80558912000000", "142.36431901000000",
]


WHOLE_COUNT = 40
"""全国件数。北海道の 34 件だけでは 6 件届かない、という状況を作るための値。"""


def whole_page(count: int) -> bytes:
    return (
        f'<table><tr><td class="searchnum">{count}件中 1件から20件のデータです。</td></tr>'
        '<form action="/utile/csv-list"><input type="hidden" name="page_no" value="1"/></form>'
        "</table>"
    ).encode()


def search_responder(whole: int = WHOLE_COUNT) -> Callable[[tuple[tuple[str, str], ...]], bytes]:
    """地域なし = 全国、北海道 = 34 件、それ以外 = 0 件 を返す応答。"""

    def respond(fields: tuple[tuple[str, str], ...]) -> bytes:
        area_name = dict(fields)["seat_pref"]
        if not area_name:
            return whole_page(whole)
        hit = area_name == HOKKAIDO.name
        return fixture("search_hit.html" if hit else "search_empty.html").encode("utf-8")

    return respond


def make_fetcher(whole: int = WHOLE_COUNT) -> FakeFetcher:
    return FakeFetcher(
        {
            INDEX_URL: fixture("search_index.html").encode("utf-8"),
            SEARCH_URL: search_responder(whole),
            CSV_URL: make_csv([SAMPLE_ROW] * 85),
        }
    )


def test_検索の送信値は分類コードと地域名() -> None:
    """分類の name は large_kind ではなく register_sub_id。地域はコードでなく名前。"""
    assert search_fields("token", CATEGORY, HOKKAIDO.name) == (
        ("_method", "POST"),
        ("_csrfToken", "token"),
        ("screen_id", "index"),
        ("page_no", "1"),
        ("register_sub_id", "102"),
        ("seat_pref", "北海道"),
    )


def test_トークン取得から_CSV_出力まで順に叩く(cache_dir: Path) -> None:
    """全国件数の 1 回ぶんを挟んでから、地域ごとの検索と CSV 出力に進む。"""
    fetcher = make_fetcher()
    fetch_ledgers(fetcher, LedgerCache(cache_dir), [CATEGORY], [HOKKAIDO])
    assert fetcher.urls() == [INDEX_URL, SEARCH_URL, SEARCH_URL, CSV_URL]
    assert dict(fetcher.calls[1][2])["seat_pref"] == ""  # 地域で絞らない = 全国


def test_CSV_出力には応答の_hidden_値をそのまま送る(cache_dir: Path) -> None:
    """推測して組み立てると 504 になる (ADR 0002)。検索の送信値の使い回しでもない。"""
    fetcher = make_fetcher()
    fetch_ledgers(fetcher, LedgerCache(cache_dir), [CATEGORY], [HOKKAIDO])
    _, _, sent = fetcher.calls[-1]
    assert sent == (
        ("_method", "POST"),
        ("_csrfToken", "d" * 128),
        ("screen_id", "index"),
        ("page_no", "1"),
        ("register_sub_id", "102"),
        ("seat_pref", "北海道"),
    )


def test_CSV_出力にフォーム固有の_hidden_値も落とさず送る(cache_dir: Path) -> None:
    """検索の送信値を組み立て直して代用しないこと。

    実サイトでは検索条件がそのまま echo されるため、代用してもたまたま通ってしまう。
    フォームにしか無い値を混ぜて、取り違えを検出できるようにする。
    """
    html = (
        '<table><tr><td class="searchnum">3件中 1件から3件のデータです。</td></tr>'
        '<form action="/utile/csv-list">'
        '<input type="hidden" name="_csrfToken" value="form-token"/>'
        '<input type="hidden" name="register_sub_id" value="102"/>'
        '<input type="hidden" name="seat_pref" value="北海道"/>'
        '<input type="hidden" name="sortTarget" value="area"/>'
        "</form></table>"
    )
    fetcher = FakeFetcher(
        {
            INDEX_URL: fixture("search_index.html").encode("utf-8"),
            SEARCH_URL: html.encode("utf-8"),
            CSV_URL: make_csv([SAMPLE_ROW] * 3),
        }
    )
    fetch_ledgers(fetcher, LedgerCache(cache_dir), [CATEGORY], [HOKKAIDO])
    assert fetcher.calls[-1][2] == (
        ("_csrfToken", "form-token"),
        ("register_sub_id", "102"),
        ("seat_pref", "北海道"),
        ("sortTarget", "area"),
    )


def test_件数と行数を単位ごとに記録する(cache_dir: Path) -> None:
    """34 は指定単位、85 は棟単位。取り違えると欠損検査が狂う。"""
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(), cache, [CATEGORY], [HOKKAIDO])
    entry = cache.entries["102/01-hokkaido"]
    assert (entry.hit_count, entry.row_count) == (34, 85)
    assert entry.byte_count > 0


def test_0_件のときは_CSV_を要求しない(cache_dir: Path) -> None:
    """フォームが無いのに推測して POST すれば 504 を踏む。"""
    fetcher = FakeFetcher(
        {
            INDEX_URL: fixture("search_index.html").encode("utf-8"),
            SEARCH_URL: search_responder(),
        }
    )
    cache = LedgerCache(cache_dir)
    fetch_ledgers(fetcher, cache, [CATEGORY], [TOKYO])
    assert CSV_URL not in fetcher.urls()
    assert cache.entries["102/13-tokyo"].row_count == 0


def test_検索応答が読めなければトークンを取り直して再試行する(cache_dir: Path) -> None:
    """長い巡回の途中でセッションが切れても、そこで全部を落とさない。"""
    responses = iter(["<html>セッション切れ</html>".encode()])

    def flaky(fields: tuple[tuple[str, str], ...]) -> bytes:
        return next(responses, None) or search_responder()(fields)

    fetcher = FakeFetcher(
        {
            INDEX_URL: fixture("search_index.html").encode("utf-8"),
            SEARCH_URL: flaky,
            CSV_URL: make_csv([SAMPLE_ROW] * 85),
        }
    )
    cache = LedgerCache(cache_dir)
    fetch_ledgers(fetcher, cache, [CATEGORY], [HOKKAIDO])
    # トークンを取り直した = トップページを 2 度取っている
    assert fetcher.urls().count(INDEX_URL) == 2
    assert cache.entries["102/01-hokkaido"].row_count == 85


def test_読めない応答が続けば諦めて失敗させる(cache_dir: Path) -> None:
    """取り直しても駄目なものを、0 件として静かに通さない。"""
    fetcher = FakeFetcher(
        {
            INDEX_URL: fixture("search_index.html").encode("utf-8"),
            SEARCH_URL: "<html>メンテナンス中</html>".encode(),
        }
    )
    with pytest.raises(ParseError):
        fetch_ledgers(fetcher, LedgerCache(cache_dir), [CATEGORY], [HOKKAIDO])


def test_中断しても取得済みをやり直さない(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(), cache, [CATEGORY], [HOKKAIDO])

    resumed = make_fetcher()
    fetch_ledgers(resumed, cache, [CATEGORY], [HOKKAIDO, TOKYO])
    # 取り直すのは未取得の東京都だけ。全国件数は毎回数え直す (網羅性の基準のため)。
    assert resumed.urls() == [INDEX_URL, SEARCH_URL, SEARCH_URL]
    assert resumed.calls[-1][2][-1] == ("seat_pref", "東京都")


def test_force_なら取得済みも取り直す(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(), cache, [CATEGORY], [HOKKAIDO])
    again = make_fetcher()
    fetch_ledgers(again, cache, [CATEGORY], [HOKKAIDO], force=True)
    assert CSV_URL in again.urls()


def test_CSV_の本体行を読む() -> None:
    rows = read_csv_rows(make_csv([SAMPLE_ROW, SAMPLE_ROW]))
    assert len(rows) == 2
    assert rows[0][:2] == ["102", "23"]


def test_BOM_付き_UTF_8_として読む() -> None:
    """Shift_JIS ではない。BOM を落とさないと先頭の列名が壊れる。"""
    raw = make_csv([SAMPLE_ROW])
    assert raw.startswith(b"\xef\xbb\xbf")
    assert read_csv_rows(raw)[0][0] == "102"


def test_列構成が違えば_504_の可能性を添えて失敗させる() -> None:
    broken = make_csv([["x"]], header=["だれかのCSV"])
    with pytest.raises(LedgerError, match="504"):
        read_csv_rows(broken)


def test_CSV_でない応答は失敗させる() -> None:
    with pytest.raises(LedgerError, match="UTF-8"):
        read_csv_rows(b"\xff\xfe\x00\x00")


def test_全国件数と地域合計を突き合わせる(cache_dir: Path) -> None:
    """どの地域でも引けない指定があると、地域合計が全国件数に届かない。"""
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(whole=40), cache, [CATEGORY], [HOKKAIDO, TOKYO])
    summary = summarize(cache, [CATEGORY], [HOKKAIDO, TOKYO])[0]

    assert summary.is_complete is True
    assert (summary.area_hit_count, summary.whole_count, summary.row_count) == (34, 40, 85)
    assert summary.difference == -6


def test_地域をまたぐ重複は正の差になる(cache_dir: Path) -> None:
    """統合時に (台帳ID, 管理対象ID) で排除する前提なので、取りこぼしとは分けて示す。"""
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(whole=30), cache, [CATEGORY], [HOKKAIDO, TOKYO])
    assert summarize(cache, [CATEGORY], [HOKKAIDO, TOKYO])[0].difference == 4


def test_全国件数は分類ごとに_1_回だけ数える(cache_dir: Path) -> None:
    fetcher = make_fetcher()
    fetch_ledgers(fetcher, LedgerCache(cache_dir), [CATEGORY], [HOKKAIDO, TOKYO])
    whole_searches = [call for call in fetcher.calls if dict(call[2]).get("seat_pref") == ""]
    assert len(whole_searches) == 1


def test_全国件数を数えていなければ差を出さない(cache_dir: Path) -> None:
    """取得前に report だけ実行した場合。無いものを 0 とみなして誤報しない。"""
    summary = summarize(LedgerCache(cache_dir), [CATEGORY], [HOKKAIDO])[0]
    assert summary.whole_count is None
    assert summary.difference is None


def test_未取得の地域があれば完了扱いにしない(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(), cache, [CATEGORY], [HOKKAIDO])
    summary = summarize(cache, [CATEGORY], [HOKKAIDO, TOKYO])[0]
    assert summary.is_complete is False
    assert summary.fetched_areas == 1


def test_取りこぼしを報告に出す(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(whole=40), cache, [CATEGORY], [HOKKAIDO, TOKYO])
    report = format_summary(summarize(cache, [CATEGORY], [HOKKAIDO, TOKYO]))
    assert "どの地域でも引けない 6 件" in report


def test_全国件数が実測時から動いていれば知らせる(cache_dir: Path) -> None:
    """新規指定・解除で動く。差そのものは異常ではないので、注記として出す。"""
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(whole=40), cache, [CATEGORY], [HOKKAIDO])
    report = format_summary(summarize(cache, [CATEGORY], [HOKKAIDO]))
    assert f"{40 - CATEGORY.known_designation_count:+,} 件変わっている" in report
