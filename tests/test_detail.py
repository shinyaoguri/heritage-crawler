"""詳細取得層のテスト。

**外部サイトへは出ない** (conftest の FakeFetcher)。ここで守りたいのは
2 万件を一度で完走しない前提 — 再開・失敗の記録・打ち切りのふるまい。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeFetcher, area_named, make_csv, make_row, put_ledger
from heritage_crawler.cache import DetailCache, DetailEntry, LedgerCache, LedgerEntry
from heritage_crawler.catalog import DESIGNATED, SEARCH_AREAS, Category
from heritage_crawler.detail import (
    DetailError,
    Presence,
    Target,
    fetch_details,
    format_detail_summary,
    format_duration,
    probe_presence,
    read_targets,
    recheck_cache,
    summarize_details,
)

CATEGORY = DESIGNATED  # 102

# 実物の詳細ページは 33〜55 KB。取得層は小さすぎる応答をエラーページとして
# 弾くので (ADR 0011)、身代わりも実物なみの大きさにする。
HTML = (
    "<html><body>琵琶湖疏水施設 第一トンネル" + "あ" * 20_000 + "</body></html>"
).encode()

MISSING = "<html><body>必要な情報が足りません。</body></html>".encode()
"""**そのレコードが無いときの応答** (2026-08-16 実測。3 KB 弱)。

同じ文言が過負荷のときにも返る (ADR 0011)。だから対照群が要る。
"""


def csv_row(kanri_taishou_id: str, name: str = "琵琶湖疏水施設", ridge: str = "") -> list[str]:
    return make_row(
        {
            "台帳ID": CATEGORY.code,
            "管理対象ID": kanri_taishou_id,
            "名称": name,
            "棟名": ridge,
        }
    )


def ledger_with(cache_dir: Path, **areas: list[list[str]]) -> LedgerCache:
    """地域ごとの CSV を持つ台帳キャッシュを組み立てる。"""
    cache = LedgerCache(cache_dir)
    for slug, rows in areas.items():
        area = next(candidate for candidate in SEARCH_AREAS if candidate.slug == slug)
        cache.record(
            CATEGORY,
            area,
            LedgerEntry(
                category_code=CATEGORY.code,
                area_name=area.name,
                hit_count=len(rows),
                row_count=len(rows),
                byte_count=0,
                fetched_at="2026-08-12T00:00:00+00:00",
            ),
            make_csv(rows),
        )
    return cache


def url(kanri_taishou_id: str) -> str:
    return f"https://kunishitei.bunka.go.jp/heritage/detail/{CATEGORY.code}/{kanri_taishou_id}"


def test_台帳の各行から取得対象を作る(cache_dir: Path) -> None:
    targets = read_targets(ledger_with(cache_dir, shiga=[csv_row("23")]), [CATEGORY])
    assert [target.key for target in targets] == ["102/23"]
    assert targets[0].url == url("23")


def test_地域をまたぐ指定は一度だけ取りに行く(cache_dir: Path) -> None:
    """102 の琵琶湖疏水施設は滋賀県と京都府の両方に現れる (#6 の実測)。"""
    cache = ledger_with(cache_dir, shiga=[csv_row("23")], kyoto=[csv_row("23"), csv_row("24")])
    assert [target.key for target in read_targets(cache, [CATEGORY])] == ["102/23", "102/24"]


def test_管理対象ID_のゼロ詰めを落とさない(cache_dir: Path) -> None:
    """int() に通すと 8 桁ゼロ詰めが崩れ、詳細ページに到達できなくなる。"""
    targets = read_targets(ledger_with(cache_dir, shiga=[csv_row("00003904")]), [CATEGORY])
    assert targets[0].url.endswith("/00003904")


def test_棟名まで含めて表示名にする(cache_dir: Path) -> None:
    cache = ledger_with(cache_dir, shiga=[csv_row("23", "琵琶湖疏水施設", "第一トンネル")])
    assert read_targets(cache, [CATEGORY])[0].name == "琵琶湖疏水施設 第一トンネル"


def test_生_HTML_を_gzip_で保存して読み戻せる(cache_dir: Path) -> None:
    cache = DetailCache(cache_dir)
    target = Target(CATEGORY.code, "23", "琵琶湖疏水施設")
    fetcher = FakeFetcher({url("23"): HTML})

    run = fetch_details([fetcher], cache, [target])

    assert (run.fetched, run.failed, run.skipped) == (1, 0, 0)
    saved = cache.html_path(CATEGORY.code, "23")
    assert saved.suffix == ".gz"
    assert saved.read_bytes() != HTML  # そのまま置いていない
    assert cache.read_html(CATEGORY.code, "23") == HTML


def test_取得済みは飛ばして再開する(cache_dir: Path) -> None:
    """6 時間級の処理は途中で落ちる。同じコマンドで続きから始められること。"""
    targets = [Target(CATEGORY.code, "23", ""), Target(CATEGORY.code, "24", "")]
    fetcher = FakeFetcher({url("23"): HTML})  # 24 は用意しない = 落ちる

    fetch_details([fetcher], DetailCache(cache_dir), targets)

    resumed = FakeFetcher({url("23"): HTML, url("24"): HTML})
    run = fetch_details([resumed], DetailCache(cache_dir), targets)

    assert run.skipped == 1
    assert resumed.urls() == [url("24")]  # 取れているぶんは叩き直さない


def test_取り直しは_force_で強制できる(cache_dir: Path) -> None:
    targets = [Target(CATEGORY.code, "23", "")]
    responses = {url("23"): HTML}
    fetch_details([FakeFetcher(responses)], DetailCache(cache_dir), targets)

    fetcher = FakeFetcher(responses)
    assert fetch_details([fetcher], DetailCache(cache_dir), targets, force=True).fetched == 1
    assert fetcher.urls() == [url("23")]


def test_HTML_の実体が消えていたら取り直す(cache_dir: Path) -> None:
    """記録だけ残って中身が無い状態を、取得済みとして通さない。"""
    cache = DetailCache(cache_dir)
    targets = [Target(CATEGORY.code, "23", "")]
    fetch_details([FakeFetcher({url("23"): HTML})], cache, targets)
    cache.html_path(CATEGORY.code, "23").unlink()

    fetcher = FakeFetcher({url("23"): HTML})
    assert fetch_details([fetcher], DetailCache(cache_dir), targets).fetched == 1


def test_失敗を記録して次へ進む(cache_dir: Path) -> None:
    """2 万件のうち 1 件が転んだだけで全体を止めない。"""
    cache = DetailCache(cache_dir)
    targets = [Target(CATEGORY.code, str(number), "") for number in (23, 24, 25)]
    fetcher = FakeFetcher({url("23"): HTML, url("25"): HTML})

    run = fetch_details([fetcher], cache, targets)

    assert (run.fetched, run.failed) == (2, 1)
    assert [entry.kanri_taishou_id for entry in cache.failures()] == ["24"]
    assert fetcher.urls() == [url("23"), url("24"), url("25")]


ERROR_PAGE = "<html><body>必要な情報が足りません。</body></html>".encode()
"""相手が **HTTP 200 で** 返すエラーページ (ADR 0011 の事故で掴んだ実物と同じ文面)。"""


def test_200_で返るエラーページは失敗として扱う(cache_dir: Path) -> None:
    """成功として記録すると取得済みになり、二度と取り直せない (ADR 0011)。"""
    cache = DetailCache(cache_dir)
    targets = [Target(CATEGORY.code, "23", ""), Target(CATEGORY.code, "24", "")]

    run = fetch_details([FakeFetcher({url("23"): HTML, url("24"): ERROR_PAGE})], cache, targets)

    assert (run.fetched, run.failed) == (1, 1)
    assert [entry.kanri_taishou_id for entry in cache.failures()] == ["24"]
    assert "必要な情報が足りません" in cache.failures()[0].error
    assert not cache.is_done(CATEGORY.code, "24")  # 取り直せる
    assert not cache.html_path(CATEGORY.code, "24").exists()  # ゴミを残さない


def test_エラーページが続けば打ち切る(cache_dir: Path) -> None:
    """レートを上げすぎたときに 2 万件ぶん叩き続けないための歯止め (ADR 0011)。"""
    targets = [Target(CATEGORY.code, str(number), "") for number in range(23, 40)]
    fetcher = FakeFetcher({target.url: ERROR_PAGE for target in targets})

    with pytest.raises(DetailError, match="続けて失敗した"):
        fetch_details([fetcher], DetailCache(cache_dir), targets, failure_limit=3)

    assert len(fetcher.urls()) == 3  # 打ち切るまでの 3 件だけ


def test_キャッシュのエラーページを検査して取り直す(cache_dir: Path) -> None:
    """気付く前に取ったぶんはキャッシュに残る。通信せずに印を外せる (ADR 0011)。"""
    cache = DetailCache(cache_dir)
    targets = [Target(CATEGORY.code, "23", ""), Target(CATEGORY.code, "24", "")]
    # エラーページを掴んでいた時代の記録を再現する (当時は ok=True で保存していた)
    for target, body in ((targets[0], HTML), (targets[1], ERROR_PAGE)):
        cache.record(
            DetailEntry(
                category_code=target.category_code,
                kanri_taishou_id=target.kanri_taishou_id,
                ok=True,
                byte_count=len(body),
                fetched_at="2026-08-12T00:00:00+00:00",
            ),
            body,
        )

    found = recheck_cache(DetailCache(cache_dir), targets)

    assert [target.kanri_taishou_id for target in found] == ["24"]
    after = DetailCache(cache_dir)
    assert after.is_done(CATEGORY.code, "23")  # 正常なぶんは触らない
    assert not after.is_done(CATEGORY.code, "24")

    # 印が外れているので、続けて取得すれば拾われる
    run = fetch_details([FakeFetcher({url("24"): HTML})], after, targets)
    assert (run.fetched, run.skipped) == (1, 1)


def test_失敗ぶんは後から拾い直せる(cache_dir: Path) -> None:
    targets = [Target(CATEGORY.code, "23", ""), Target(CATEGORY.code, "24", "")]
    fetch_details([FakeFetcher({url("23"): HTML})], DetailCache(cache_dir), targets)

    cache = DetailCache(cache_dir)
    retry = [
        Target(entry.category_code, entry.kanri_taishou_id, "") for entry in cache.failures()
    ]
    run = fetch_details([FakeFetcher({url("24"): HTML})], cache, retry)

    assert run.fetched == 1
    assert DetailCache(cache_dir).failures() == []


def test_続けて失敗したら打ち切る(cache_dir: Path) -> None:
    """相手が落ちているか弾かれている。気付かずに 2 万回叩き続けない。"""
    cache = DetailCache(cache_dir)
    targets = [Target(CATEGORY.code, str(number), "") for number in range(10, 20)]
    fetcher = FakeFetcher({})

    with pytest.raises(DetailError, match="続けて失敗"):
        fetch_details([fetcher], cache, targets, failure_limit=3)

    assert len(fetcher.urls()) == 3  # 残りは叩かない


def test_成功を挟めば打ち切らない(cache_dir: Path) -> None:
    targets = [Target(CATEGORY.code, str(number), "") for number in (23, 24, 25)]
    fetcher = FakeFetcher({url("24"): HTML})

    run = fetch_details([fetcher], DetailCache(cache_dir), targets, failure_limit=2)

    assert (run.fetched, run.failed) == (1, 2)


def test_並列でも全件を取る(cache_dir: Path) -> None:
    """並列度は fetcher の数で決まる。レートは共有の RateLimiter が抑える。"""
    cache = DetailCache(cache_dir)
    targets = [Target(CATEGORY.code, str(number), "") for number in range(10, 30)]
    responses = {target.url: HTML for target in targets}
    fetchers = [FakeFetcher(responses) for _ in range(4)]

    run = fetch_details(fetchers, cache, targets)

    assert run.fetched == 20
    assert sum(len(fetcher.urls()) for fetcher in fetchers) == 20
    assert len(DetailCache(cache_dir).entries) == 20


def test_並列でも打ち切りは効く(cache_dir: Path) -> None:
    targets = [Target(CATEGORY.code, str(number), "") for number in range(10, 40)]
    fetchers = [FakeFetcher({}) for _ in range(2)]

    with pytest.raises(DetailError):
        fetch_details(fetchers, DetailCache(cache_dir), targets, failure_limit=3)

    # 並列ぶんの行き違いは許すが、残り 30 件を叩き切ることは無い
    assert sum(len(fetcher.urls()) for fetcher in fetchers) < 10


def test_取得する_fetcher_が無ければ受け付けない(cache_dir: Path) -> None:
    with pytest.raises(ValueError):
        fetch_details([], DetailCache(cache_dir), [])


def test_取得状況を報告する(cache_dir: Path) -> None:
    cache = DetailCache(cache_dir)
    targets = [Target(CATEGORY.code, str(number), "") for number in (23, 24, 25)]
    fetch_details([FakeFetcher({url("23"): HTML})], cache, targets[:2])

    summary = summarize_details(DetailCache(cache_dir), targets)

    assert (summary.total, summary.fetched, summary.failed, summary.missing) == (3, 1, 1, 1)
    assert summary.is_complete is False
    printed = format_detail_summary(summary, DetailCache(cache_dir).failures())
    assert "対象 3 件" in printed
    assert "--retry-failed" in printed


class Test存在の確認:
    """削除候補が本当にデータベースから消えているかを直接確かめる (ADR 0021)。

    **「必要な情報が足りません」は過負荷のときにも返る** (ADR 0011 の 4 req/s 実測)。
    「そのレコードは無い」と「いま答えられない」が同じ文言なので、**対照群**
    — 実在すると分かっているキー — を検査の前後に引いて時制を担保する。
    """

    control = Target(CATEGORY.code, "control", "対照群")
    gone = Target(CATEGORY.code, "9999", "消えたはず")
    alive = Target(CATEGORY.code, "2594", "生きている")

    def fetcher(self, **bodies: bytes) -> FakeFetcher:
        return FakeFetcher(
            {Target(CATEGORY.code, key, "").url: body for key, body in bodies.items()}
        )

    def test_対照群が正常なら無いという返答を信じる(self) -> None:
        fetcher = self.fetcher(control=HTML, **{"9999": MISSING, "2594": HTML})

        found = probe_presence(fetcher, [self.gone, self.alive], self.control)

        assert found == {
            f"{CATEGORY.code}/9999": Presence.GONE,
            f"{CATEGORY.code}/2594": Presence.ALIVE,
        }
        # 対照群は前後で 1 回ずつ (2 件の検査に対して 4 リクエスト)
        assert len(fetcher.urls()) == 4

    def test_対照群が壊れていたら検査そのものをしない(self) -> None:
        """混んでいる時間帯に当たった 1 件を「削除された」と記録しないため。"""
        fetcher = self.fetcher(control=MISSING, **{"9999": MISSING})

        found = probe_presence(fetcher, [self.gone], self.control)

        assert found == {f"{CATEGORY.code}/9999": Presence.UNKNOWN}
        # 相手が答えられない時間帯に、無駄なリクエストを積まない
        assert len(fetcher.urls()) == 1

    def test_検査の後で対照群が崩れたら結論を取り下げる(self) -> None:
        """検査中に相手が混み始めた回。前の対照群だけでは時制を担保できない。"""
        answers = iter([HTML, MISSING])
        fetcher = FakeFetcher(
            {
                self.control.url: lambda _: next(answers),
                self.gone.url: MISSING,
            }
        )

        found = probe_presence(fetcher, [self.gone], self.control)

        assert found == {f"{CATEGORY.code}/9999": Presence.UNKNOWN}

    def test_取得に失敗したものは分からないままにする(self) -> None:
        fetcher = self.fetcher(control=HTML)  # 9999 の応答を用意していない

        found = probe_presence(fetcher, [self.gone], self.control)

        assert found == {f"{CATEGORY.code}/9999": Presence.UNKNOWN}

    def test_キャッシュには何も書かない(self, cache_dir: Path) -> None:
        """`fetch_details` を流用すると、消えた行が「失敗」として積まれる。

        `--retry-failed` が毎週それを叩き、連続失敗の打ち切りが誤発動する。
        """
        cache = DetailCache(cache_dir)
        fetcher = self.fetcher(control=HTML, **{"9999": MISSING})

        probe_presence(fetcher, [self.gone], self.control)

        assert DetailCache(cache_dir).entries == {}
        assert not cache.manifest_path.exists()


def test_台帳が空なら先に何をすべきか言う() -> None:
    printed = format_detail_summary(summarize_details(DetailCache(Path("なし")), []))
    assert "fetch-ledger" in printed


@pytest.mark.parametrize(
    ("seconds", "expected"), [(45.0, "45 秒"), (600.0, "10 分"), (20461.0, "5.7 時間")]
)
def test_残り時間は桁を落として読ませる(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected


# --- 台帳ID と分類コードが食い違う分類 (#74) ---

OTHER_LEDGER = Category("411", "登録記念物", 148)
"""台帳ID (401) と分類コード (411) が違う分類 (2026-08-23 実測)。

現行 4 分類はこの 2 つが同値なので、取り違えても表に出なかった。
"""


def ledger_with_other(cache_dir: Path) -> LedgerCache:
    """台帳ID 401 の行を分類 411 として置いた台帳キャッシュ。"""
    cache = LedgerCache(cache_dir)
    put_ledger(
        cache,
        OTHER_LEDGER,
        area_named("北海道"),
        ["00003483"],
        values={"台帳ID": "401", "名称": "函館公園"},
    )
    return cache


def test_台帳ID_ではなく分類コードで詳細ページを引く(cache_dir: Path) -> None:
    """URL の第 1 セグメントは分類コード (#74)。

    登録記念物 (411) の台帳ID は 401 で、401 で組むと「必要な情報が足りません」の
    エラーページが返る。
    """
    targets = read_targets(ledger_with_other(cache_dir), [OTHER_LEDGER])
    assert [target.url for target in targets] == [
        "https://kunishitei.bunka.go.jp/heritage/detail/411/00003483"
    ]


def test_台帳ID_が違ってもキャッシュのキーは分類コードで揃う(cache_dir: Path) -> None:
    """キャッシュは取りに行った分類ごとに分ける。台帳ID では 401 と混ざる。"""
    targets = read_targets(ledger_with_other(cache_dir), [OTHER_LEDGER])
    assert [target.key for target in targets] == ["411/00003483"]
