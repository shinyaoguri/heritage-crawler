"""キャッシュとマニフェストのテスト。中断からの再開がここに掛かっている。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heritage_crawler.cache import (
    DETAIL_MANIFEST_VERSION,
    MANIFEST_VERSION,
    DetailCache,
    DetailEntry,
    LedgerCache,
    LedgerEntry,
)
from heritage_crawler.catalog import SEARCH_AREAS, TARGET_CATEGORIES

CATEGORY = TARGET_CATEGORIES[1]  # 102
HOKKAIDO = SEARCH_AREAS[0]
TOKYO = SEARCH_AREAS[12]


def entry(**overrides: object) -> LedgerEntry:
    values: dict[str, object] = {
        "category_code": CATEGORY.code,
        "area_name": HOKKAIDO.name,
        "hit_count": 34,
        "row_count": 85,
        "byte_count": 26780,
        "fetched_at": "2026-08-11T10:00:00+00:00",
    }
    values.update(overrides)
    return LedgerEntry(**values)  # type: ignore[arg-type]


def test_保存先は_ASCII_のファイル名にする(cache_dir: Path) -> None:
    path = LedgerCache(cache_dir).csv_path(CATEGORY, HOKKAIDO)
    assert path == cache_dir / "ledger" / "102" / "01-hokkaido.csv"
    assert path.name.isascii()


def test_回収した行は地域別と別のファイルに置く(cache_dir: Path) -> None:
    """地域別 (<コード>-<slug>.csv) と名前が衝突しない置き場にする (ADR 0017)。"""
    cache = LedgerCache(cache_dir)
    path = cache.recovered_csv_path(CATEGORY)
    assert path == cache_dir / "ledger" / "102" / "recovered.csv"
    assert path != cache.csv_path(CATEGORY, HOKKAIDO)


def test_取得した_CSV_とマニフェストを書く(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    cache.record(CATEGORY, HOKKAIDO, entry(), b"\xef\xbb\xbfcsv")

    assert cache.csv_path(CATEGORY, HOKKAIDO).read_bytes() == b"\xef\xbb\xbfcsv"
    saved = json.loads(cache.manifest_path.read_text(encoding="utf-8"))
    assert saved["version"] == MANIFEST_VERSION
    assert saved["entries"]["102/01-hokkaido"]["row_count"] == 85


def test_別のインスタンスからマニフェストを読み直せる(cache_dir: Path) -> None:
    """再開は別プロセスから始まる。書いた内容がそのまま読めることが前提。"""
    LedgerCache(cache_dir).record(CATEGORY, HOKKAIDO, entry(), b"csv")
    assert LedgerCache(cache_dir).entries["102/01-hokkaido"].hit_count == 34


def test_全国件数もマニフェストに残す(cache_dir: Path) -> None:
    """網羅性の基準。再開後の report でも同じ判定ができるよう永続化する。"""
    LedgerCache(cache_dir).record_whole_count(CATEGORY, 2633)
    assert LedgerCache(cache_dir).whole_counts == {"102": 2633}


def test_取得済みは飛ばす(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    assert cache.is_done(CATEGORY, HOKKAIDO) is False
    cache.record(CATEGORY, HOKKAIDO, entry(), b"csv")
    assert cache.is_done(CATEGORY, HOKKAIDO) is True


def test_0_件は_CSV_が無くても取得済みとみなす(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    cache.record(CATEGORY, TOKYO, entry(area_name=TOKYO.name, hit_count=0, row_count=0), b"")
    assert cache.csv_path(CATEGORY, TOKYO).exists() is False
    assert cache.is_done(CATEGORY, TOKYO) is True


def test_CSV_の実体が消えていたら取り直す(cache_dir: Path) -> None:
    """記録だけ残って中身が無い状態を、取得済みとして通さない。"""
    cache = LedgerCache(cache_dir)
    cache.record(CATEGORY, HOKKAIDO, entry(), b"csv")
    cache.csv_path(CATEGORY, HOKKAIDO).unlink()
    assert cache.is_done(CATEGORY, HOKKAIDO) is False


def test_書き換えの一時ファイルを残さない(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    cache.record(CATEGORY, HOKKAIDO, entry(), b"csv")
    assert list(cache.ledger_dir.rglob("*.tmp")) == []


def test_知らない形式のマニフェストは黙って使わない(cache_dir: Path) -> None:
    cache = LedgerCache(cache_dir)
    cache.manifest_path.parent.mkdir(parents=True)
    cache.manifest_path.write_text(json.dumps({"version": 99, "entries": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="未対応"):
        _ = cache.entries


def detail_entry(**overrides: object) -> DetailEntry:
    values: dict[str, object] = {
        "category_code": "102",
        "kanri_taishou_id": "00003904",
        "ok": True,
        "byte_count": 47_000,
        "fetched_at": "2026-08-12T10:00:00+00:00",
    }
    values.update(overrides)
    return DetailEntry(**values)  # type: ignore[arg-type]


def test_詳細の保存先は分類コード_で分ける(cache_dir: Path) -> None:
    path = DetailCache(cache_dir).html_path("102", "00003904")
    assert path == cache_dir / "detail" / "102" / "00003904.html.gz"


def test_パスに使えない_ID_は取り込まない(cache_dir: Path) -> None:
    with pytest.raises(ValueError, match="ファイル名"):
        DetailCache(cache_dir).html_path("102", "../../etc/passwd")


def test_詳細のマニフェストは追記して読み直せる(cache_dir: Path) -> None:
    """2 万件を 1 件ずつ全体書き直しすると重い。追記したものが読めること。"""
    cache = DetailCache(cache_dir)
    cache.record(detail_entry(), b"<html></html>")
    cache.record(detail_entry(kanri_taishou_id="23"), b"<html></html>")

    lines = cache.manifest_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"version": DETAIL_MANIFEST_VERSION}
    assert len(lines) == 3
    assert sorted(DetailCache(cache_dir).entries) == ["102/00003904", "102/23"]


def test_同じ対象は最後の結果で判断する(cache_dir: Path) -> None:
    """失敗のあとに成功したら、成功として扱う (再試行が効く)。"""
    cache = DetailCache(cache_dir)
    cache.record(detail_entry(ok=False, byte_count=0, error="504"))
    cache.record(detail_entry(), b"<html></html>")

    assert DetailCache(cache_dir).failures() == []
    assert DetailCache(cache_dir).is_done("102", "00003904") is True


def test_失敗の記録は取得済みにしない(cache_dir: Path) -> None:
    cache = DetailCache(cache_dir)
    cache.record(detail_entry(ok=False, byte_count=0, error="404 を返した"))

    assert cache.is_done("102", "00003904") is False
    assert [entry.error for entry in cache.failures()] == ["404 を返した"]


def test_欠けた行は飛ばして残りを読む(cache_dir: Path) -> None:
    """追記の途中で電源が落ちると末尾の行が欠ける。全体を捨てない。"""
    cache = DetailCache(cache_dir)
    cache.record(detail_entry(), b"<html></html>")
    with cache.manifest_path.open("a", encoding="utf-8") as manifest:
        manifest.write('{"category_code": "102", "kanri_tai')

    assert list(DetailCache(cache_dir).entries) == ["102/00003904"]


def test_知らない形式の詳細マニフェストは黙って使わない(cache_dir: Path) -> None:
    cache = DetailCache(cache_dir)
    cache.detail_dir.mkdir(parents=True)
    cache.manifest_path.write_text(json.dumps({"version": 99}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="未対応"):
        _ = cache.entries


def test_旧名で書かれたマニフェストも読める(cache_dir: Path) -> None:
    """``daichou_id`` は分類コードの誤った呼び名だった (#74)。

    現行 4 分類では値が同じなので、読み替えるだけで手元の 23,742 件を
    取り直さずに済む。
    """
    cache = DetailCache(cache_dir)
    cache.detail_dir.mkdir(parents=True)
    cache.manifest_path.write_text(
        json.dumps({"version": DETAIL_MANIFEST_VERSION})
        + "\n"
        + json.dumps(
            {
                "daichou_id": "102",
                "kanri_taishou_id": "00003904",
                "ok": True,
                "byte_count": 47_000,
                "fetched_at": "2026-08-12T10:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    entries = DetailCache(cache_dir).entries
    assert list(entries) == ["102/00003904"]
    assert entries["102/00003904"].category_code == "102"
