"""詳細ページの HTML から、原文の項目をそのまま読み取る (ADR 0008 の 1 段目)。

ここではスキーマを決めない。**原文のラベルと値の対応**、附指定、解説文、
関連情報の有無だけを取り出し、キーの正規化は ``record`` に任せる。読み取りと
スキーマを分けておくと、サイト側の表記が変わったときに直す場所が 1 つで済む。

ページの構造 (2026-08-12 の実データで確認):

- 主情報は ``<tr>`` の 3 セル (ラベル / ``：`` / 値)。ラベルは分類ごとに違う
- 解説文と詳細解説はモーダルの ``<textarea>``。見出し (``解説文`` / ``詳細解説``)
  が何の文章かを表す。解説文は本文側の欄にも同じものが出る
- ``detail_rellist_N_M`` のモーダルにはラベルと値の組が入る。中身は分類で変わる
  (建造物系は附指定、401 は指定等後に行った措置の履歴)
- 添付ファイルなどの有無は ``relatedinformation`` の表に ``なし`` か
  リンクとして出る

正規表現ではなく HTML パーサを使うのは ``search_page`` と同じ理由に加えて、
**ラベルが数値文字参照で書かれている**ことがあるため
(本文側の解説文の見出しは ``&#35299;&#35500;&#25991;``)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Final

from heritage_crawler.search_page import ParseError

# ParseError は search_page と共有する。「取得できたが期待した形をしていない」は
# 検索ページでも詳細ページでも同じ扱い (呼び出し側は 1 つ捕まえれば足りる)。
__all__ = [
    "DESCRIPTION_TITLE",
    "DETAILED_DESCRIPTION_TITLE",
    "DetailPage",
    "ParseError",
    "parse_detail_page",
]

DESCRIPTION_TITLE: Final = "解説文"
DETAILED_DESCRIPTION_TITLE: Final = "詳細解説"

_SEPARATORS: Final = ("：", ":")
_MODAL_ID_PREFIX: Final = "detail_"
_PHOTO_MODAL_ID: Final = "detail_photolist"
_RELLIST_MODAL_ID: Final = re.compile(r"detail_rellist_\d+_\d+")
_RELATED_TABLE_ID: Final = "relatedinformation"
_NO_INFORMATION: Final = "なし"
"""関連情報の欄に出る「無い」の表記。あるときは値ではなくリンクになる。"""


@dataclass(frozen=True)
class DetailPage:
    """詳細ページから読み取った原文。キーはすべてページ上のラベルのまま。"""

    fields: dict[str, str]
    """主情報のラベル → 値。空の項目は落としてある。"""

    texts: dict[str, str] = field(default_factory=dict)
    """``解説文`` / ``詳細解説`` の見出し → 本文。改行はそのまま保つ。"""

    related: dict[str, bool] = field(default_factory=dict)
    """関連情報のラベル → 有無 (``附指定`` / ``指定等後に行った措置`` / ``添付ファイル``)。"""

    rellists: tuple[dict[str, str], ...] = ()
    """``detail_rellist_*`` モーダル 1 件ぶんのラベル → 値。

    **何の一覧かは分類で変わる。** 建造物系は附指定 (``附名称`` / ``附員数``)、
    401 は指定等後に行った措置の履歴 (``異動年月日`` / ``異動種別1`` ほか)。
    ここでは区別せず、意味付けは ``record`` に任せる。
    """

    has_photo: bool = False
    """写真の有無。画像そのものは扱わない (ADR 0007)。"""

    @property
    def description(self) -> str:
        return self.texts.get(DESCRIPTION_TITLE, "")

    @property
    def detailed_description(self) -> str:
        return self.texts.get(DETAILED_DESCRIPTION_TITLE, "")


@dataclass
class _Cell:
    classes: tuple[str, ...]
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join("".join(self.parts).split())


class _DetailParser(HTMLParser):
    """表の行・モーダルの本文・関連情報を、入れ子を保ったまま拾う。

    表が入れ子になっているため、行とセルはスタックで持つ。テキストは常に一番
    内側のセルへ入れ、閉じたセルは一番内側の行へ渡す。こうしないと外側の行に
    中の表の文字列が丸ごと混ざる。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self.texts: dict[str, str] = {}
        self.related: dict[str, bool] = {}
        self.rellists: dict[str, dict[str, str]] = {}
        self.has_photo = False
        self.rows_seen = 0
        self._rows: list[list[_Cell]] = []
        self._cells: list[_Cell] = []
        self._divs: list[tuple[int, str]] = []
        self._div_depth = 0
        self._tables: list[tuple[int, str]] = []
        self._table_depth = 0
        self._textarea: list[str] | None = None
        self._titles: dict[str, str] = {}
        self._bodies: list[tuple[str, str]] = []

    # --- 現在地 ---------------------------------------------------------

    @property
    def _modal(self) -> str:
        """今いるモーダル。

        モーダルの中には ``id="contena"`` の入れ物がさらに入っているので、
        一番内側の id ではなく ``detail_`` で始まる id を内側から探す。
        """
        for _, identifier in reversed(self._divs):
            if identifier.startswith(_MODAL_ID_PREFIX):
                return identifier
        return ""

    @property
    def _table(self) -> str:
        return self._tables[-1][1] if self._tables else ""

    # --- タグ -----------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag == "div":
            self._div_depth += 1
            if identifier := values.get("id"):
                self._divs.append((self._div_depth, identifier))
                if identifier == _PHOTO_MODAL_ID:
                    self.has_photo = True
        elif tag == "table":
            self._table_depth += 1
            if identifier := values.get("id"):
                self._tables.append((self._table_depth, identifier))
        elif tag == "tr":
            self._rows.append([])
        elif tag == "td":
            self._cells.append(_Cell(classes=tuple(values.get("class", "").split())))
        elif tag == "textarea":
            self._textarea = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "div":
            if self._divs and self._divs[-1][0] == self._div_depth:
                self._divs.pop()
            self._div_depth = max(self._div_depth - 1, 0)
        elif tag == "table":
            if self._tables and self._tables[-1][0] == self._table_depth:
                self._tables.pop()
            self._table_depth = max(self._table_depth - 1, 0)
        elif tag == "td" and self._cells:
            cell = self._cells.pop()
            if "resulttitle00" in cell.classes:
                self._titles[self._modal] = cell.text
            if self._rows:
                self._rows[-1].append(cell)
        elif tag == "tr" and self._rows:
            self._handle_row(self._rows.pop())
        elif tag == "textarea" and self._textarea is not None:
            self._bodies.append(
                (self._modal, "".join(self._textarea).replace("\r\n", "\n").strip())
            )
            self._textarea = None

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._textarea.append(data)
        elif self._cells:
            self._cells[-1].parts.append(data)

    # --- 行の振り分け ---------------------------------------------------

    def _handle_row(self, cells: list[_Cell]) -> None:
        texts = [cell.text for cell in cells]
        if self._table == _RELATED_TABLE_ID:
            self._handle_related_row(texts)
        elif _RELLIST_MODAL_ID.fullmatch(self._modal):
            if _is_pair(texts):
                self.rellists.setdefault(self._modal, {})[texts[0]] = texts[2]
        elif _is_pair(texts):
            self.rows_seen += 1
            if texts[2]:
                self.fields[texts[0]] = texts[2]
        elif len(texts) == 2 and texts[0].startswith(DESCRIPTION_TITLE) and texts[1]:
            # 本文側の解説文。モーダルの textarea と同じ文章だが、そちらが
            # 無いページのために拾っておく (改行はモーダル側にしか無い)。
            self.texts.setdefault(DESCRIPTION_TITLE, texts[1])

    def _handle_related_row(self, texts: list[str]) -> None:
        if len(texts) != 3:
            return
        label = texts[1]
        if not label or label == "(情報の有無)":
            return
        self.related[label] = texts[2] != _NO_INFORMATION

    # --- 仕上げ ---------------------------------------------------------

    def close(self) -> None:
        super().close()
        for modal, body in self._bodies:
            title = self._titles.get(modal)
            if title and body:
                # 本文側の解説文より、改行の残る textarea を優先する。
                self.texts[title] = body


def _is_pair(texts: list[str]) -> bool:
    """ラベル / 区切り / 値 の 3 セル構成か。"""
    return len(texts) == 3 and texts[1] in _SEPARATORS


def parse_detail_page(html: str) -> DetailPage:
    """詳細ページ 1 枚を読む。

    項目が 1 つも無ければ ``ParseError``。エラーページや構成変更を、空の
    レコードとして黙って通さないため。
    """
    parser = _DetailParser()
    parser.feed(html)
    parser.close()

    if not parser.rows_seen:
        raise ParseError(
            "詳細ページに主情報の項目が 1 つも無い。"
            "エラーページを掴んでいるか、ページの構成が変わった可能性がある"
        )
    return DetailPage(
        fields=parser.fields,
        texts=parser.texts,
        related=parser.related,
        rellists=tuple(parser.rellists[key] for key in sorted(parser.rellists, key=_rellist_order)),
        has_photo=parser.has_photo,
    )


def _rellist_order(modal_id: str) -> tuple[int, ...]:
    """``detail_rellist_1_10`` が ``detail_rellist_1_2`` の後に来るように数で並べる。"""
    return tuple(int(number) for number in re.findall(r"\d+", modal_id))
