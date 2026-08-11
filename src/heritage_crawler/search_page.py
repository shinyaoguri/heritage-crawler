"""検索ページの HTML から、次のリクエストに必要な値を取り出す。

CSV 出力の POST パラメータは**推測して組み立てず、応答 HTML の csv-list フォームの
hidden 値をそのまま送る**。推測した値を送ると 504 Gateway Timeout になる
(ADR 0002 / Issue #1 の調査所見。CSV 機能自体の障害と誤認しかけた落とし穴)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Final

CSV_FORM_ACTION: Final = "/utile/csv-list"
CSRF_FIELD: Final = "_csrfToken"

_HIT_COUNT_RE: Final = re.compile(r"([\d,]+)\s*件中")


class ParseError(ValueError):
    """HTML が期待した形をしていない。サイト側の変更を疑う。"""


@dataclass(frozen=True)
class SearchPage:
    """検索応答から読み取った、次の手に必要なものだけ。"""

    hit_count: int
    """件数表示の総数。**指定**単位で、CSV の行数 (棟単位) とは一致しない。"""

    csv_fields: tuple[tuple[str, str], ...] | None
    """csv-list フォームの hidden 値。0 件のときはフォームごと無いので None。"""


@dataclass
class _Form:
    action: str
    fields: list[tuple[str, str]] = field(default_factory=list)


class _PageParser(HTMLParser):
    """フォームの hidden 値と件数表示だけを拾う。

    正規表現ではなく HTML パーサを使うのは、属性の順序や引用符の揺れで
    静かに取りこぼすのを避けるため。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self.hit_count_text: str | None = None
        self._current_form: _Form | None = None
        self._hit_count_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "form":
            self._current_form = _Form(action=values.get("action", ""))
        elif tag == "input" and self._current_form is not None:
            name = values.get("name", "")
            if name and values.get("type", "").lower() == "hidden":
                self._current_form.fields.append((name, values.get("value", "")))
        elif tag == "td" and "searchnum" in values.get("class", "").split():
            self._hit_count_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current_form is not None:
            self.forms.append(self._current_form)
            self._current_form = None
        elif tag == "td" and self._hit_count_parts is not None:
            self.hit_count_text = "".join(self._hit_count_parts)
            self._hit_count_parts = None

    def handle_data(self, data: str) -> None:
        if self._hit_count_parts is not None:
            self._hit_count_parts.append(data)


def _parse(html: str) -> _PageParser:
    parser = _PageParser()
    parser.feed(html)
    parser.close()
    return parser


def extract_csrf_token(html: str) -> str:
    """検索トップページから CSRF トークンを取り出す。

    トークンは Cookie と対になっているため、取得したクライアントで
    そのまま送り続けること。
    """
    for form in _parse(html).forms:
        for name, value in form.fields:
            if name == CSRF_FIELD and value:
                return value
    raise ParseError(f"{CSRF_FIELD} が見つからない。検索ページの構成が変わった可能性がある")


def parse_search_page(html: str) -> SearchPage:
    """検索応答から件数と CSV 出力用の hidden 値を取り出す。"""
    parser = _parse(html)

    if parser.hit_count_text is None:
        raise ParseError("件数表示 (td.searchnum) が見つからない。検索が成立していない可能性がある")
    matched = _HIT_COUNT_RE.search(parser.hit_count_text)
    if matched is None:
        raise ParseError(f"件数表示から総数を読み取れない: {parser.hit_count_text.strip()!r}")
    hit_count = int(matched.group(1).replace(",", ""))

    csv_fields: tuple[tuple[str, str], ...] | None = None
    for form in parser.forms:
        if form.action.endswith(CSV_FORM_ACTION):
            csv_fields = tuple(form.fields)
            break

    # 0 件のときは csv-list フォームごと出力されない (実測)。ここで CSV を
    # 要求しないことが、推測した値を送って 504 を踏まないことにもつながる。
    if hit_count > 0 and csv_fields is None:
        raise ParseError(
            f"{hit_count} 件あるのに {CSV_FORM_ACTION} フォームが無い。"
            "ページの構成が変わった可能性がある"
        )
    return SearchPage(hit_count=hit_count, csv_fields=csv_fields)
