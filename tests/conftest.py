"""テスト共通の道具。

**外部サイトへは一切アクセスしない** (CLAUDE.md)。応答はすべて
``tests/fixtures/`` に置いた実応答の切り出しか、ここで組み立てたもので賄う。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from heritage_crawler.http import FetchError, FormFields
from heritage_crawler.ledger import EXPECTED_CSV_HEADER

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_csv(rows: Sequence[Sequence[str]], header: Sequence[str] = EXPECTED_CSV_HEADER) -> bytes:
    """実物と同じ UTF-8 (BOM 付き)・LF の CSV を組み立てる。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_ALL)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


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
