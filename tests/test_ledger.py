"""台帳取得層のテスト。

FakeFetcher を挟んでいるので外部サイトへは出ない (CLAUDE.md)。
応答 HTML は 2026-08-11 の実物から切り出したフィクスチャ。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import FakeFetcher, fixture, make_csv, put_ledger, put_recovered
from heritage_crawler.cache import LedgerCache
from heritage_crawler.catalog import DESIGNATED, MONUMENTS, SEARCH_AREAS
from heritage_crawler.ledger import (
    AREA_COLUMN_INDEX,
    AREA_COLUMN_LABELS,
    CSV_URL,
    EXPECTED_CSV_HEADER,
    INDEX_URL,
    SEARCH_URL,
    LedgerError,
    fetch_ledgers,
    format_summary,
    read_csv_rows,
    search_fields,
    summarize,
)

CATEGORY = DESIGNATED  # 102 (1 指定が複数の棟に展開される)
UNEXPANDED = MONUMENTS  # 401 (指定 = 1 行)
HOKKAIDO = SEARCH_AREAS[0]
AOMORI = SEARCH_AREAS[1]
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


NO_WAIT: Callable[[float], None] = lambda _: None  # noqa: E731 - 再試行の待ちを飛ばす


BROKEN_RESPONSE = "<html>必要な情報が足りません。</html>".encode()
"""200 で返る壊れた応答 (ADR 0011)。件数表示が無いので読めない。"""


def broken_fetcher(*areas: str, times: int | None = None) -> FakeFetcher:
    """指定した地域の検索だけが壊れた応答を返す取得の身代わり。

    ``times`` を渡すと、その回数だけ壊れて以降は正常に答える (相手が回復する形)。
    地域名に ``""`` を渡すと全国件数の検索が壊れる。
    """
    normal = search_responder()
    remaining = times

    def respond(fields: tuple[tuple[str, str], ...]) -> bytes:
        nonlocal remaining
        if dict(fields)["seat_pref"] in areas and remaining != 0:
            if remaining is not None:
                remaining -= 1
            return BROKEN_RESPONSE
        return normal(fields)

    return FakeFetcher(
        {
            INDEX_URL: fixture("search_index.html").encode("utf-8"),
            SEARCH_URL: respond,
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
    fetch_ledgers(fetcher, cache, [CATEGORY], [HOKKAIDO], sleep=NO_WAIT)
    # トークンを取り直した = トップページを 2 度取っている
    assert fetcher.urls().count(INDEX_URL) == 2
    assert cache.entries["102/01-hokkaido"].row_count == 85


def test_読めない応答には待ちを挟んで繰り返す(cache_dir: Path) -> None:
    """読めない応答は 200 で返るので、HTTP クライアント側の再試行が働かない。

    トークンの取り直しだけでは相手の一時的な不調に効かないため、間を置いて粘る
    (ADR 0022)。待ち時間は http.RETRY_BACKOFF と同じく 2 秒・4 秒。
    """
    waits: list[float] = []
    cache = LedgerCache(cache_dir)
    run = fetch_ledgers(
        broken_fetcher(HOKKAIDO.name, times=2), cache, [CATEGORY], [HOKKAIDO], sleep=waits.append
    )

    assert waits == [2.0, 4.0]  # 3 回目で読めた
    assert run.ok
    assert cache.entries["102/01-hokkaido"].row_count == 85


def test_読めない応答が続けば取れなかったものとして数える(cache_dir: Path) -> None:
    """取り直しても駄目なものを、0 件として静かに通さない。"""
    cache = LedgerCache(cache_dir)
    run = fetch_ledgers(
        broken_fetcher(HOKKAIDO.name), cache, [CATEGORY], [HOKKAIDO], sleep=NO_WAIT
    )

    assert run.ok is False
    assert run.failures == ["102 × 北海道"]
    assert "102/01-hokkaido" not in cache.entries


def test_1_地域が取れなくても残りを取りに行く(cache_dir: Path) -> None:
    """止めてしまうと、繰り返しても同じところで死んで 1 地域も前へ進めない。

    2026-08-16 の週次実行がそうなった (102 × 三重県 で 3 回とも即死。Issue #53)。
    """
    cache = LedgerCache(cache_dir)
    areas = [HOKKAIDO, AOMORI, TOKYO]
    run = fetch_ledgers(
        broken_fetcher(AOMORI.name), cache, [CATEGORY], areas, sleep=NO_WAIT
    )

    assert run.failures == ["102 × 青森県"]
    # 壊れた地域の前も後も取れている = 次の回に残るのは青森県だけ
    assert cache.entries["102/01-hokkaido"].row_count == 85
    assert cache.entries["102/13-tokyo"].row_count == 0


def test_全国件数が取れなくても地域は取りに行く(cache_dir: Path) -> None:
    """全国件数が無いのは網羅性を判定できないだけ。地域の取得は最後まで進む。"""
    cache = LedgerCache(cache_dir)
    run = fetch_ledgers(
        broken_fetcher(""), cache, [CATEGORY], [HOKKAIDO], sleep=NO_WAIT
    )

    assert run.failures == ["102 の全国件数"]
    assert cache.entries["102/01-hokkaido"].row_count == 85
    assert summarize(cache, [CATEGORY], [HOKKAIDO])[0].whole_count is None


def test_全国件数も取得済みなら数え直さない(cache_dir: Path) -> None:
    """取れているものを叩き直すと、そこで新しく失敗しうる (ADR 0027)。

    2026-08-23 の週次実行がそうなった。3 回目の試行で台帳の CSV は全 19 分類ぶん
    揃ったのに、毎回叩き直していた 101 の全国件数だけが取れず、それだけで
    fetch-ledger が 1 を返して 1 週ぶんの更新が飛んだ (Issue #92)。
    """
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(), cache, [CATEGORY], [HOKKAIDO])

    # 全国件数が壊れる相手でも、取り直しに行かないので影響を受けない
    resumed = broken_fetcher("")
    run = fetch_ledgers(resumed, cache, [CATEGORY], [HOKKAIDO, TOKYO], sleep=NO_WAIT)

    assert run.ok
    assert [dict(sent)["seat_pref"] for _, url, sent in resumed.calls if url == SEARCH_URL] == [
        TOKYO.name
    ]
    assert cache.whole_counts[CATEGORY.code] == WHOLE_COUNT


def test_全国件数が取れなかった回の次は数え直す(cache_dir: Path) -> None:
    """飛ばしてよいのは取れているものだけ。取れなかった回は次が拾いに行く。"""
    cache = LedgerCache(cache_dir)
    fetch_ledgers(broken_fetcher(""), cache, [CATEGORY], [HOKKAIDO], sleep=NO_WAIT)

    run = fetch_ledgers(make_fetcher(), cache, [CATEGORY], [HOKKAIDO])

    assert run.ok
    assert cache.whole_counts[CATEGORY.code] == WHOLE_COUNT


def test_続けて失敗したら打ち切って残りを叩かない(cache_dir: Path) -> None:
    """相手が落ちているのに 204 地域を叩き続けない。"""
    areas = list(SEARCH_AREAS[:6])
    fetcher = broken_fetcher(*(area.name for area in areas))
    run = fetch_ledgers(fetcher, LedgerCache(cache_dir), [CATEGORY], areas, sleep=NO_WAIT)

    assert len(run.failures) == 5  # CONSECUTIVE_FAILURE_LIMIT
    assert run.abort_reason is not None
    searched = [dict(sent)["seat_pref"] for _, url, sent in fetcher.calls if url == SEARCH_URL]
    assert areas[5].name not in searched


def test_中断しても取得済みをやり直さない(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(), cache, [CATEGORY], [HOKKAIDO])

    resumed = make_fetcher()
    fetch_ledgers(resumed, cache, [CATEGORY], [HOKKAIDO, TOKYO])
    # 取り直すのは未取得の東京都だけ (全国件数も取得済みなら飛ばす。ADR 0027)。
    assert resumed.urls() == [INDEX_URL, SEARCH_URL]
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


@pytest.mark.parametrize("label", sorted(AREA_COLUMN_LABELS))
def test_地域の列名は分類で変わる(label: str) -> None:
    """12 列目の見出しだけが分類で変わる (2026-08-23 実測。#74)。

    ``都道府県`` (101 系) / ``所有者住所（所在都道府県）`` (美術工芸品) /
    ``地域`` (無形文化財) / ``都道府県、地域`` (無形民俗文化財)。
    """
    header = list(EXPECTED_CSV_HEADER)
    header[AREA_COLUMN_INDEX] = label

    rows = read_csv_rows(make_csv([SAMPLE_ROW], header=header))

    assert rows[0][:2] == ["102", "23"]


def test_知らない地域の列名は今までどおり弾く() -> None:
    """緩めるのは 12 列目だけ。列構成そのものの変化は見逃さない。"""
    header = list(EXPECTED_CSV_HEADER)
    header[AREA_COLUMN_INDEX] = "所在の県"
    with pytest.raises(LedgerError, match="504"):
        read_csv_rows(make_csv([SAMPLE_ROW], header=header))


def test_他の列の見出しが変われば弾く() -> None:
    """12 列目を緩めたせいで他の列の変化まで通してしまわないこと。"""
    header = list(EXPECTED_CSV_HEADER)
    header[2] = "名前"
    with pytest.raises(LedgerError, match="504"):
        read_csv_rows(make_csv([SAMPLE_ROW], header=header))


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


def test_回収済みなら取りこぼしの注記にそう書く(cache_dir: Path) -> None:
    """回収しても件数表示の差は動かない (回収ぶんは地域の件数に入らない)。

    黙っていると穴が残っているように読める。件数が一致しないのは、差が件数表示
    どうしの引き算で相殺しうる一方、回収数は一覧で名指しした実数だから (ADR 0026)。
    """
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(whole=40), cache, [CATEGORY], [HOKKAIDO, TOKYO])
    put_recovered(cache, CATEGORY, ["9001", "9002"])

    report = format_summary(summarize(cache, [CATEGORY], [HOKKAIDO, TOKYO]))
    assert "どの地域でも引けない 6 件 (一覧から 2 件回収済み)" in report


def test_件数表示では相殺して消える取りこぼしをキーの異なり数で暴く(cache_dir: Path) -> None:
    """重複と取りこぼしが同数あると、件数表示どうしの差はどちらも見せない。

    401 の初回取得で実際に起きた形 (Issue #28)。重複 34 と取りこぼし 1 が
    「重複 33」に化けていた。
    """
    cache = LedgerCache(cache_dir)
    put_ledger(cache, UNEXPANDED, HOKKAIDO, ["1", "2", "3"])
    put_ledger(cache, UNEXPANDED, TOKYO, ["3", "4"])  # 3 が両方の地域に現れる
    cache.record_whole_count(UNEXPANDED, 5)  # 手元にあるのは 4 件

    summary = summarize(cache, [UNEXPANDED], [HOKKAIDO, TOKYO])[0]

    assert (summary.row_count, summary.unique_key_count) == (5, 4)
    assert summary.duplicate_rows == 1
    assert summary.difference == 0  # 件数表示どうしの差では相殺して見えない
    assert summary.missing_count == 1


class Test網羅性の判定:
    """``looks_complete`` は差分更新で「消えた指定を落としてよいか」を決める (ADR 0018)。

    行を消す判断に使うので、**迷ったら偽**にする。
    """

    def test_全部取れていれば真(self, cache_dir: Path) -> None:
        cache = LedgerCache(cache_dir)
        put_ledger(cache, UNEXPANDED, HOKKAIDO, ["1", "2"])
        put_ledger(cache, UNEXPANDED, TOKYO, ["3"])
        cache.record_whole_count(UNEXPANDED, 3)

        assert summarize(cache, [UNEXPANDED], [HOKKAIDO, TOKYO])[0].looks_complete is True

    def test_地域をまたぐ重複は取りこぼしではない(self, cache_dir: Path) -> None:
        """102 の琵琶湖疏水施設のような正の差。**これで消せなくなるのは行き過ぎ**。"""
        cache = LedgerCache(cache_dir)
        fetch_ledgers(make_fetcher(whole=30), cache, [CATEGORY], [HOKKAIDO, TOKYO])
        summary = summarize(cache, [CATEGORY], [HOKKAIDO, TOKYO])[0]

        assert summary.difference == 4
        assert summary.note == "地域をまたぐ重複 4 件"
        assert summary.looks_complete is True

    @pytest.mark.parametrize(
        ("whole", "reason"),
        [(5, "どの地域でも引けない"), (2, "全国件数より")],
        ids=["取りこぼし", "数え方が怪しい"],
    )
    def test_全国件数と食い違えば偽(self, cache_dir: Path, whole: int, reason: str) -> None:
        cache = LedgerCache(cache_dir)
        put_ledger(cache, UNEXPANDED, HOKKAIDO, ["1", "2", "3"])
        cache.record_whole_count(UNEXPANDED, whole)

        summary = summarize(cache, [UNEXPANDED], [HOKKAIDO])[0]

        assert reason in summary.note
        assert summary.looks_complete is False

    def test_未取得の地域があれば偽(self, cache_dir: Path) -> None:
        cache = LedgerCache(cache_dir)
        put_ledger(cache, UNEXPANDED, HOKKAIDO, ["1"])
        cache.record_whole_count(UNEXPANDED, 1)

        assert summarize(cache, [UNEXPANDED], [HOKKAIDO, TOKYO])[0].looks_complete is False

    def test_全国件数を数えていなければ偽(self, cache_dir: Path) -> None:
        """突き合わせる基準が無い状態。取れているとは言えない。"""
        cache = LedgerCache(cache_dir)
        put_ledger(cache, UNEXPANDED, HOKKAIDO, ["1"])

        assert summarize(cache, [UNEXPANDED], [HOKKAIDO])[0].looks_complete is False


def test_全国件数より多ければ数え方を疑うよう促す(cache_dir: Path) -> None:
    """取りこぼしと逆向きの差。件数表示が指定単位でない可能性を示す。"""
    cache = LedgerCache(cache_dir)
    put_ledger(cache, UNEXPANDED, HOKKAIDO, ["1", "2", "3"])
    cache.record_whole_count(UNEXPANDED, 2)

    report = format_summary(summarize(cache, [UNEXPANDED], [HOKKAIDO]))
    assert "全国件数より 1 件多い" in report


def test_棟に展開される分類では異なり数で取りこぼしを判定しない(cache_dir: Path) -> None:
    """102 は 1 指定が複数の棟になるので、異なり数 (棟) と全国件数 (指定) は比べられない。"""
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(whole=40), cache, [CATEGORY], [HOKKAIDO, TOKYO])
    summary = summarize(cache, [CATEGORY], [HOKKAIDO, TOKYO])[0]

    assert summary.unique_key_count == 1  # 同じ棟が 85 行 (フィクスチャの都合)
    assert summary.missing_count is None
    assert summary.difference == -6  # こちらは従来どおり見える


def test_未取得の地域があれば取りこぼしより先にそれを知らせる(cache_dir: Path) -> None:
    """取り終えていない段階の異なり数は少なくて当たり前。取りこぼしと混同させない。"""
    cache = LedgerCache(cache_dir)
    put_ledger(cache, UNEXPANDED, HOKKAIDO, ["1"])
    cache.record_whole_count(UNEXPANDED, 5)

    report = format_summary(summarize(cache, [UNEXPANDED], [HOKKAIDO, TOKYO]))
    assert "未取得 1 地域" in report
    assert "どの地域でも引けない" not in report


def test_取りこぼしをキーの異なり数から報告に出す(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    put_ledger(cache, UNEXPANDED, HOKKAIDO, ["1", "2"])
    cache.record_whole_count(UNEXPANDED, 3)

    report = format_summary(summarize(cache, [UNEXPANDED], [HOKKAIDO]))
    assert "どの地域でも引けない 1 件" in report


def test_全国件数が実測時から動いていれば知らせる(cache_dir: Path) -> None:
    """新規指定・解除で動く。差そのものは異常ではないので、注記として出す。"""
    cache = LedgerCache(cache_dir)
    fetch_ledgers(make_fetcher(whole=40), cache, [CATEGORY], [HOKKAIDO])
    report = format_summary(summarize(cache, [CATEGORY], [HOKKAIDO]))
    assert f"{40 - CATEGORY.known_designation_count:+,} 件変わっている" in report
