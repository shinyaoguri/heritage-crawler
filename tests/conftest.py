"""テスト共通の道具。

**外部サイトへは一切アクセスしない** (CLAUDE.md)。応答はすべて
``tests/fixtures/`` に置いた実応答の切り出しか、ここで組み立てたもので賄う。
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from heritage_crawler.cache import DetailCache, DetailEntry, LedgerCache, LedgerEntry
from heritage_crawler.catalog import SEARCH_AREAS, Area, Category
from heritage_crawler.http import FetchError, FormFields
from heritage_crawler.ledger import EXPECTED_CSV_HEADER

FIXTURES = Path(__file__).parent / "fixtures"

FETCHED_AT = "2026-08-12T00:00:00+00:00"
"""取得日時の既定値。日付そのものを試すとき以外はこれで足りる。"""


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_row(values: Mapping[str, str]) -> list[str]:
    """CSV の 1 行を列名で組み立てる。指定しなかった列は空にする。"""
    unknown = set(values) - set(EXPECTED_CSV_HEADER)
    if unknown:
        raise AssertionError(f"CSV に無い列名を指定した: {sorted(unknown)}")
    return [values.get(column, "") for column in EXPECTED_CSV_HEADER]


def make_csv(rows: Sequence[Sequence[str]], header: Sequence[str] = EXPECTED_CSV_HEADER) -> bytes:
    """実物と同じ UTF-8 (BOM 付き)・LF の CSV を組み立てる。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_ALL)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def area_named(name: str) -> Area:
    """検索の分割軸を表示名で引く。"""
    return next(area for area in SEARCH_AREAS if area.name == name)


def put_ledger(
    cache: LedgerCache,
    category: Category,
    area: Area,
    ids: Sequence[str],
    fetched_at: str = FETCHED_AT,
    values: Mapping[str, str] | None = None,
) -> None:
    """台帳 CSV をキャッシュへ置く。管理対象ID だけを指定した行を並べる。

    ``values`` を渡すと全行に同じ列を足せる (緯度経度など、CSV にしかない項目)。
    """
    rows = [
        make_row({"台帳ID": category.code, "管理対象ID": managed_id, **(values or {})})
        for managed_id in ids
    ]
    cache.record(
        category,
        area,
        LedgerEntry(
            category_code=category.code,
            area_name=area.name,
            hit_count=len(rows),
            row_count=len(rows),
            byte_count=0,
            fetched_at=fetched_at,
        ),
        make_csv(rows),
    )


def put_detail(
    cache: DetailCache,
    category: Category,
    managed_id: str,
    html: str,
    fetched_at: str = FETCHED_AT,
) -> None:
    """詳細ページ 1 枚をキャッシュへ置く。"""
    cache.record(
        DetailEntry(
            daichou_id=category.code,
            kanri_taishou_id=managed_id,
            ok=True,
            byte_count=len(html),
            fetched_at=fetched_at,
        ),
        html.encode("utf-8"),
    )


def lines(path: Path) -> list[dict[str, Any]]:
    """書き出した JSON Lines を 1 行 1 レコードで読む。"""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


Responder = Callable[[FormFields], bytes]


class FakeFetcher:
    """URL ごとに決めた応答を返し、呼ばれた順を記録する取得の身代わり。

    送信値で応答を変えたいときは bytes の代わりに関数を渡す。用意していない
    URL を叩いたら失敗させる — 余計なリクエストを黙って見逃さないため。
    """

    def __init__(self, responses: dict[str, bytes | Responder]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []

    def get(self, url: str) -> bytes:
        self.calls.append(("GET", url, ()))
        return self._body(url, ())

    def post(self, url: str, fields: FormFields) -> bytes:
        self.calls.append(("POST", url, tuple(fields)))
        return self._body(url, fields)

    def _body(self, url: str, fields: FormFields) -> bytes:
        response = self._responses.get(url)
        if response is None:
            raise FetchError(f"用意していない応答を求められた: {url}")
        return response(fields) if callable(response) else response

    def urls(self, method: str | None = None) -> list[str]:
        return [url for verb, url, _ in self.calls if method is None or verb == method]


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"
