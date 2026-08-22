"""一覧の収集・突き合わせ・回収のテスト。

FakeFetcher を挟んでいるので外部サイトへは出ない (CLAUDE.md)。
応答 HTML は 2026-08-12 の実物から切り出したフィクスチャ。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from conftest import FakeFetcher, Responder, fixture, put_ledger
from heritage_crawler.cache import LedgerCache
from heritage_crawler.catalog import (
    DESIGNATED,
    MONUMENTS,
    REGISTERED_MONUMENTS,
    SEARCH_AREAS,
)
from heritage_crawler.http import FormFields
from heritage_crawler.ledger import (
    INDEX_URL,
    SEARCH_URL,
    Session,
    read_csv_rows,
    read_ledger_rows,
)
from heritage_crawler.listing import (
    Listing,
    ListingAudit,
    ListingError,
    audit_listing,
    fetch_listing,
    format_audits,
    recover_missing,
)
from heritage_crawler.search_page import ListingRow, ParseError

CATEGORY = MONUMENTS  # 401 (指定 = 1 行。一覧と突き合わせられる)
EXPANDED = DESIGNATED  # 102 (1 指定が複数の棟に展開される)
HOKKAIDO = SEARCH_AREAS[0]

LISTED_KEYS = ["2", "3420", "00003542", "1979", "4001"]
"""フィクスチャ 2 ページぶんに出てくる管理対象ID。"""

PAGES = ["listing_p1.html", "listing_p2.html"]


def paging_responder(pages: list[str]) -> Responder:
    """``pageNumber`` で求められたページを返す。無指定なら 1 ページ目。"""

    def respond(fields: FormFields) -> bytes:
        number = int(dict(fields).get("pageNumber", "1"))
        return fixture(pages[number - 1]).encode("utf-8")

    return respond


def _index() -> bytes:
    return fixture("search_index.html").encode("utf-8")


def make_fetcher(pages: list[str] | None = None) -> FakeFetcher:
    return FakeFetcher(
        {
            INDEX_URL: _index(),
            SEARCH_URL: paging_responder(pages or PAGES),
        }
    )


def collect(pages: list[str] | None = None) -> Listing:
    fetcher = make_fetcher(pages)
    return fetch_listing(fetcher, Session(fetcher), CATEGORY)


def test_最後のページまで辿ってキーを集める() -> None:
    assert [row.kanri_taishou_id for row in collect().rows] == LISTED_KEYS


def test_表示件数を最大にしてから辿る() -> None:
    """20 件のままだと相手へのリクエストが 5 倍になる。"""
    fetcher = make_fetcher()
    fetch_listing(fetcher, Session(fetcher), CATEGORY)
    sent = [dict(fields) for _, url, fields in fetcher.calls if url == SEARCH_URL]
    assert sent[0].get("pageSize") is None  # 最初の検索
    assert sent[1]["pageSize"] == "100"  # 表示件数フォーム + pageSize
    assert sent[2]["pageNumber"] == "2"  # 2 ページ目
    assert len(sent) == 3


def test_ページ送りの送信値は応答のフォームをそのまま使う() -> None:
    """推測して組み立てない (ADR 0002)。"""
    fetcher = make_fetcher()
    fetch_listing(fetcher, Session(fetcher), CATEGORY)
    last = dict(fetcher.calls[-1][2])
    assert last["sortTarget"] == "area"
    assert last["_csrfToken"] == "dddd"


def test_表示件数フォームが無ければ既定のまま辿る() -> None:
    """相手の作りが変わっても、遅くなるだけで止まらないようにする。"""
    without_select = fixture("listing_p1.html").replace('<select name="pageSize">', "<select>")
    pages = {1: without_select, 2: fixture("listing_p2.html")}

    def respond(fields: FormFields) -> bytes:
        return pages[int(dict(fields).get("pageNumber", "1"))].encode("utf-8")

    fetcher = FakeFetcher({INDEX_URL: _index(), SEARCH_URL: respond})
    listing = fetch_listing(fetcher, Session(fetcher), CATEGORY)
    assert len(listing.rows) == len(LISTED_KEYS)
    assert all("pageSize" not in dict(fields) for _, _, fields in fetcher.calls)


def test_同じページが返り続けたら重複として報告する() -> None:
    listing = collect(["listing_p1.html", "listing_p1.html"])
    assert listing.duplicate_keys == ("401/2", "401/3420", "401/00003542")
    assert not listing.is_consistent


def test_次のページが尽きなければ打ち切る() -> None:
    """相手に無駄なリクエストを浴びせ続けない。"""

    def respond(fields: FormFields) -> bytes:
        number = int(dict(fields).get("pageNumber", "1"))
        return (
            '<table><tr><td class="searchnum">3件中 1件から1件のデータです。</td></tr></table>'
            '<table class="result"><tr><td class="result-th">名称</td></tr>'
            f'<tr><td><a href="/heritage/detail/401/{number}">名前</a></td></tr></table>'
            '<form action="/bsys/searchlist">'
            f'<input type="hidden" name="pageNumber" value="{number + 1}"/></form>'
        ).encode()

    fetcher = FakeFetcher({INDEX_URL: _index(), SEARCH_URL: respond})
    with pytest.raises(ListingError, match="空回り"):
        fetch_listing(fetcher, Session(fetcher), CATEGORY)


def test_表示件数を変えて空になったら失敗させる() -> None:
    def respond(fields: FormFields) -> bytes:
        widened = dict(fields).get("pageSize")
        return fixture("search_empty.html" if widened else "listing_p1.html").encode("utf-8")

    fetcher = FakeFetcher({INDEX_URL: _index(), SEARCH_URL: respond})
    with pytest.raises(ParseError, match="表示件数"):
        fetch_listing(fetcher, Session(fetcher), CATEGORY)


def test_棟に展開される分類は突き合わせられない() -> None:
    """一覧は指定単位、CSV は棟単位。数えるものが違うので、通信する前に断る。"""
    fetcher = make_fetcher()
    with pytest.raises(ListingError, match="棟"):
        fetch_listing(fetcher, Session(fetcher), EXPANDED)
    assert fetcher.calls == []


def test_台帳に無い指定を取りこぼしとして名指しする(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    put_ledger(cache, CATEGORY, HOKKAIDO, ["2", "3420", "1979", "4001"])
    audit = audit_listing(cache, collect(), SEARCH_AREAS)
    assert [row.key for row in audit.missing] == ["401/00003542"]
    assert audit.unexpected == ()
    assert audit.ledger_key_count == 4


def test_一覧から消えたキーは余りとして出す(cache_dir: Path) -> None:
    """指定解除の直後などに起こりうる。捨てずに報告へ回す。"""
    cache = LedgerCache(cache_dir)
    put_ledger(cache, CATEGORY, HOKKAIDO, [*LISTED_KEYS, "9999"])
    audit = audit_listing(cache, collect(), SEARCH_AREAS)
    assert audit.missing == ()
    assert audit.unexpected == ("401/9999",)


def test_回収した行を台帳として読み直せる(cache_dir: Path) -> None:
    """回収 CSV は地域別と同じ 18 列。読む側は区別しなくてよい。"""
    cache = LedgerCache(cache_dir)
    put_ledger(cache, CATEGORY, HOKKAIDO, ["2", "3420", "1979", "4001"])
    assert recover_missing(cache, audit_listing(cache, collect(), SEARCH_AREAS)) == 1

    rows = [row for row in read_ledger_rows(cache, [CATEGORY]) if row.key == "401/00003542"]
    assert len(rows) == 1
    assert rows[0].get("名称") == "智頭往来 志戸坂峠越"
    assert (rows[0].get("緯度"), rows[0].get("経度")) == (
        "35.20917500000000",
        "134.32870500000000",
    )
    assert rows[0].get("都道府県") == ""


def test_回収_CSV_は決定的(cache_dir: Path) -> None:
    """同じ入力なら同じバイト列。データが動かない月に差分を立てないため。"""
    cache = LedgerCache(cache_dir)
    put_ledger(cache, CATEGORY, HOKKAIDO, ["2"])
    audit = audit_listing(cache, collect(), SEARCH_AREAS)
    recover_missing(cache, audit)
    first = cache.recovered_csv_path(CATEGORY).read_bytes()
    recover_missing(cache, audit)
    assert cache.recovered_csv_path(CATEGORY).read_bytes() == first


def test_取りこぼしが無くなれば回収_CSV_は消える(cache_dir: Path) -> None:
    """元データの都道府県が埋まれば地域別で引ける。古い行を残さない。"""
    cache = LedgerCache(cache_dir)
    put_ledger(cache, CATEGORY, HOKKAIDO, ["2"])
    recover_missing(cache, audit_listing(cache, collect(), SEARCH_AREAS))
    put_ledger(cache, CATEGORY, HOKKAIDO, LISTED_KEYS)

    assert recover_missing(cache, audit_listing(cache, collect(), SEARCH_AREAS)) == 0
    assert not cache.recovered_csv_path(CATEGORY).exists()


def test_報告は取りこぼしの理由まで出す(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    put_ledger(cache, CATEGORY, HOKKAIDO, ["2", "3420", "1979"])
    report = format_audits([audit_listing(cache, collect(), SEARCH_AREAS)])
    assert "401/00003542  智頭往来 志戸坂峠越  (地域欄が空)" in report
    assert "401/4001  座標の無い指定  (地域欄は「地域を定めない」・緯度経度も無い)" in report


def test_一覧が件数表示と合わなければ報告に出す(cache_dir: Path) -> None:
    """集め損ねに気付かずに「網羅した」と言わないため。"""
    cache = LedgerCache(cache_dir)
    put_ledger(cache, CATEGORY, HOKKAIDO, LISTED_KEYS)
    partial = dataclasses.replace(collect(), rows=collect().rows[:2])
    assert "件数表示と合わない" in format_audits([audit_listing(cache, partial, SEARCH_AREAS)])


def test_回収した行の台帳ID_は分類側から採る(cache_dir: Path) -> None:
    """一覧が持つのは分類コードで、台帳ID ではない (#74)。

    登録記念物 (411) の一覧リンクは ``/heritage/detail/411/…`` だが、台帳ID 列に
    入るべき値は 401。ここを取り違えると、地域別 CSV から来た同じ指定と別キーになり、
    差分更新で「追加」と「削除」に化ける。
    """
    cache = LedgerCache(cache_dir)
    row = ListingRow(
        category_code="411", kanri_taishou_id="00003483", name="函館公園", area="北海道"
    )
    audit = ListingAudit(
        listing=Listing(
            category=REGISTERED_MONUMENTS, hit_count=1, rows=(row,), duplicate_keys=()
        ),
        ledger_key_count=0,
        missing=(row,),
        unexpected=(),
    )

    recover_missing(cache, audit)

    written = read_csv_rows(cache.recovered_csv_path(REGISTERED_MONUMENTS).read_bytes())
    assert written[0][:2] == ["401", "00003483"]
