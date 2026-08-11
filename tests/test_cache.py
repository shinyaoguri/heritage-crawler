"""キャッシュとマニフェストのテスト。中断からの再開がここに掛かっている。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heritage_crawler.cache import MANIFEST_VERSION, LedgerCache, LedgerEntry
from heritage_crawler.catalog import BUILDING_CATEGORIES, SEARCH_AREAS

CATEGORY = BUILDING_CATEGORIES[1]  # 102
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
