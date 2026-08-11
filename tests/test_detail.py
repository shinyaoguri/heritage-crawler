"""詳細取得層のテスト。

**外部サイトへは出ない** (conftest の FakeFetcher)。ここで守りたいのは
2 万件を一度で完走しない前提 — 再開・失敗の記録・打ち切りのふるまい。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeFetcher, make_csv, make_row
from heritage_crawler.cache import DetailCache, LedgerCache, LedgerEntry
from heritage_crawler.catalog import BUILDING_CATEGORIES, SEARCH_AREAS
from heritage_crawler.detail import (
    DetailError,
    Target,
    fetch_details,
    format_detail_summary,
    format_duration,
    read_targets,
    summarize_details,
)

CATEGORY = BUILDING_CATEGORIES[1]  # 102

HTML = "<html><body>琵琶湖疏水施設 第一トンネル</body></html>".encode()


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


def test_失敗ぶんは後から拾い直せる(cache_dir: Path) -> None:
    targets = [Target(CATEGORY.code, "23", ""), Target(CATEGORY.code, "24", "")]
    fetch_details([FakeFetcher({url("23"): HTML})], DetailCache(cache_dir), targets)

    cache = DetailCache(cache_dir)
    retry = [
        Target(entry.daichou_id, entry.kanri_taishou_id, "") for entry in cache.failures()
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


def test_台帳が空なら先に何をすべきか言う() -> None:
    printed = format_detail_summary(summarize_details(DetailCache(Path("なし")), []))
    assert "fetch-ledger" in printed


@pytest.mark.parametrize(
    ("seconds", "expected"), [(45.0, "45 秒"), (600.0, "10 分"), (20461.0, "5.7 時間")]
)
def test_残り時間は桁を落として読ませる(seconds: float, expected: str) -> None:
    assert format_duration(seconds) == expected
