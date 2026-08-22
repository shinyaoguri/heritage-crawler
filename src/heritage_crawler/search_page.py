"""検索ページの HTML から、次のリクエストに必要な値を取り出す。

CSV 出力の POST パラメータは**推測して組み立てず、応答 HTML の csv-list フォームの
hidden 値をそのまま送る**。推測した値を送ると 504 Gateway Timeout になる
(ADR 0002 / Issue #1 の調査所見。CSV 機能自体の障害と誤認しかけた落とし穴)。
同じ理由で、ページ送りも**ページャのフォームの hidden 値をそのまま送る**
(``parse_listing_page``)。

検索結果一覧そのものを読む口もここに置く (ADR 0017)。CSV では引けない指定が
一覧には出るため、網羅性の最終確認はこちらでしかできない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Final

CSV_FORM_ACTION: Final = "/utile/csv-list"
CSRF_FIELD: Final = "_csrfToken"

DETAIL_PATH: Final = "/heritage/detail/"
"""一覧の行から詳細ページへ張られるリンク。``/{台帳ID}/{管理対象ID}`` が続く。"""

PAGE_NUMBER_FIELD: Final = "pageNumber"
"""ページ送りの番号。**``page_no`` ではない。**

検索フォームにも ``page_no`` があるが、こちらは常に 1 のまま送られる飾りで、
値を変えても 1 ページ目が返る (2026-08-12 の実測)。
"""

PAGE_SIZE_FIELD: Final = "pageSize"
MAX_PAGE_SIZE: Final = "100"
"""表示件数の select が提供する最大値。20 のままだと 5 倍のリクエストになる。"""

AREA_COLUMN_LABEL: Final = "都道府県"
"""一覧の地域列の見出し。実際は「都道府県、地域▲ ※美工品は…」と続く。"""

_HEADER_CLASS: Final = "result-th"
"""結果表の見出しセル。検索フォーム側の表と混ざらないよう完全一致で見る。"""

_HIT_COUNT_RE: Final = re.compile(r"([\d,]+)\s*件中")

_MAP_CALL_RE: Final = re.compile(
    r"mapChange\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)"
)
"""地図表示ボタンの ``mapChange(台帳ID, 管理対象ID, 緯度, 経度)``。"""


class ParseError(ValueError):
    """HTML が期待した形をしていない。サイト側の変更を疑う。"""


@dataclass(frozen=True)
class SearchPage:
    """検索応答から読み取った、次の手に必要なものだけ。"""

    hit_count: int
    """件数表示の総数。**指定**単位で、CSV の行数 (棟単位) とは一致しない。"""

    csv_fields: tuple[tuple[str, str], ...] | None
    """csv-list フォームの hidden 値。0 件のときはフォームごと無いので None。"""


@dataclass(frozen=True)
class ListingRow:
    """検索結果一覧の 1 行 = 1 **指定**。CSV の 1 行 (棟) とは単位が違う。"""

    category_code: str
    """詳細ページのリンクの第 1 セグメント。**台帳ID ではない** (#74)。

    現行 4 分類では台帳ID と同値だが、登録記念物 (411) の台帳ID は 401 になる。
    """

    kanri_taishou_id: str
    name: str
    area: str
    """一覧の地域列。**空のことがある** — その指定はどの seat_pref でも引けない
    (2026-08-12 実測。Issue #28)。"""

    latitude: str = ""
    longitude: str = ""
    """地図表示ボタンが持つ座標。**CSV の緯度経度と同じ値** (2026-08-12 実測)。

    CSV が下流へ渡しているのは緯度経度だけなので、CSV で引けない指定は
    ここから台帳の行を組み立て直せる (ADR 0017)。地図表示が無い行では空。
    """

    @property
    def key(self) -> str:
        """``(分類コード, 管理対象ID)``。

        現行 4 分類では ``LedgerRow.key`` (台帳ID ベース) と同じ形になるので
        そのまま突き合わせられる。台帳ID が分類コードと食い違う分類を足すときは、
        突き合わせる側で揃える必要がある (#74)。
        """
        return f"{self.category_code}/{self.kanri_taishou_id}"


@dataclass(frozen=True)
class ListingPage:
    """一覧 1 ページぶん。次のページを取るのに要るものまで含む。"""

    hit_count: int
    rows: tuple[ListingRow, ...]

    page_size_fields: tuple[tuple[str, str], ...] | None
    """表示件数フォームの hidden 値。``pageSize`` を添えて送り直すと件数が変わる。"""

    pager_fields: dict[str, tuple[tuple[str, str], ...]]
    """ページ番号 → そのページを開くフォームの hidden 値。最終ページには次が無い。"""

    def fields_for_page(self, number: int) -> tuple[tuple[str, str], ...] | None:
        return self.pager_fields.get(str(number))


@dataclass
class _Form:
    action: str
    fields: list[tuple[str, str]] = field(default_factory=list)
    selects: list[str] = field(default_factory=list)
    """このフォームが持つ select の name。表示件数フォームを見分けるのに使う。"""

    def value_of(self, name: str) -> str | None:
        for field_name, value in self.fields:
            if field_name == name:
                return value
        return None


@dataclass
class _Cell:
    """表の 1 セル。見出しか本文かは class で見分ける。"""

    header: bool
    colspan: int
    text_parts: list[str] = field(default_factory=list)
    link: str = ""
    """このセルにあった詳細ページへのリンク (無ければ空)。"""

    coordinates: tuple[str, str] | None = None
    """地図表示ボタンから読んだ (緯度, 経度)。"""

    @property
    def text(self) -> str:
        return " ".join("".join(self.text_parts).split())


class _PageParser(HTMLParser):
    """フォームの hidden 値・件数表示・結果表の行を拾う。

    正規表現ではなく HTML パーサを使うのは、属性の順序や引用符の揺れで
    静かに取りこぼすのを避けるため。見出しセルの中にも並べ替え用のフォームが
    入れ子になっているので、フォームとセルは独立に集める。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[_Form] = []
        self.hit_count_text: str | None = None
        self.header_cells: list[_Cell] = []
        self.data_rows: list[list[_Cell]] = []
        self._current_form: _Form | None = None
        self._hit_count_parts: list[str] | None = None
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        classes = values.get("class", "").split()
        if tag == "form":
            self._current_form = _Form(action=values.get("action", ""))
        elif tag == "input" and self._current_form is not None:
            name = values.get("name", "")
            if name and values.get("type", "").lower() == "hidden":
                self._current_form.fields.append((name, values.get("value", "")))
        elif tag == "select" and self._current_form is not None:
            self._current_form.selects.append(values.get("name", ""))
        elif tag == "tr":
            self._row = []
        elif tag == "td":
            if "searchnum" in classes:
                self._hit_count_parts = []
            self._cell = _Cell(
                header=_HEADER_CLASS in classes,
                colspan=_positive_int(values.get("colspan", "1")),
            )
        elif tag == "a" and self._cell is not None and not self._cell.link:
            href = values.get("href", "")
            if href.startswith(DETAIL_PATH):
                self._cell.link = href
        elif (
            tag == "button"
            and self._cell is not None
            and self._cell.coordinates is None
            # 属性値の中身は JavaScript の呼び出しなので、ここだけは正規表現で読む。
            and (matched := _MAP_CALL_RE.search(values.get("onclick", "")))
        ):
            self._cell.coordinates = (matched.group(1), matched.group(2))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._current_form is not None:
            self.forms.append(self._current_form)
            self._current_form = None
        elif tag == "td":
            if self._hit_count_parts is not None:
                self.hit_count_text = "".join(self._hit_count_parts)
                self._hit_count_parts = None
            if self._cell is not None and self._row is not None:
                self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._close_row(self._row)
            self._row = None

    def _close_row(self, row: list[_Cell]) -> None:
        """見出し行と本文行だけを残す。検索フォームなど他の表の行は捨てる。"""
        if any(cell.header for cell in row):
            # 見出しは 1 度だけ拾う (結果表は 1 つ。以降に同じ形の表が出ても上書きしない)
            if not self.header_cells:
                self.header_cells = row
        elif any(cell.link for cell in row):
            self.data_rows.append(row)

    def handle_data(self, data: str) -> None:
        if self._hit_count_parts is not None:
            self._hit_count_parts.append(data)
        if self._cell is not None:
            self._cell.text_parts.append(data)


def _positive_int(value: str) -> int:
    """colspan の値。読めなければ 1 として扱う (欠けても列がずれるだけにする)。"""
    try:
        return max(1, int(value))
    except ValueError:
        return 1


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


def _hit_count(parser: _PageParser) -> int:
    if parser.hit_count_text is None:
        raise ParseError("件数表示 (td.searchnum) が見つからない。検索が成立していない可能性がある")
    matched = _HIT_COUNT_RE.search(parser.hit_count_text)
    if matched is None:
        raise ParseError(f"件数表示から総数を読み取れない: {parser.hit_count_text.strip()!r}")
    return int(matched.group(1).replace(",", ""))


def parse_search_page(html: str) -> SearchPage:
    """検索応答から件数と CSV 出力用の hidden 値を取り出す。"""
    parser = _parse(html)
    hit_count = _hit_count(parser)

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


def _area_column(header: list[_Cell]) -> int | None:
    """見出しから地域列が何セル目かを求める。colspan があるので数えて足す。"""
    position = 0
    for cell in header:
        if cell.text.startswith(AREA_COLUMN_LABEL):
            return position
        position += cell.colspan
    return None


def _rows(parser: _PageParser) -> tuple[ListingRow, ...]:
    area_column = _area_column(parser.header_cells)
    rows = []
    for cells in parser.data_rows:
        # 同じ行に 2 つリンクがある (矢印画像と名称)。名前が取れる方を採る。
        linked = [cell for cell in cells if cell.link]
        named = next((cell for cell in linked if cell.text), linked[0])
        ids = named.link.removeprefix(DETAIL_PATH).split("/")
        if len(ids) != 2 or not all(ids):
            raise ParseError(f"詳細ページのリンクを読めない: {named.link!r}")
        latitude, longitude = next(
            (cell.coordinates for cell in cells if cell.coordinates), ("", "")
        )
        rows.append(
            ListingRow(
                category_code=ids[0],
                kanri_taishou_id=ids[1],
                name=named.text,
                area=_cell_at(cells, area_column),
                latitude=latitude,
                longitude=longitude,
            )
        )
    return tuple(rows)


def _cell_at(cells: list[_Cell], column: int | None) -> str:
    """何セル目かではなく何列目かで引く (colspan を数える)。"""
    if column is None:
        return ""
    position = 0
    for cell in cells:
        if position == column:
            return cell.text
        position += cell.colspan
    return ""


def parse_listing_page(html: str) -> ListingPage:
    """検索結果一覧から、行とページ送りに要る hidden 値を取り出す。

    ページ送りとページサイズの値は**推測せず、応答の中のフォームをそのまま使う**
    (CSV と同じ理由。ADR 0002)。
    """
    parser = _parse(html)
    hit_count = _hit_count(parser)
    rows = _rows(parser)

    if hit_count > 0 and not rows:
        raise ParseError(
            f"{hit_count} 件あるのに一覧の行が 1 つも読めない。ページの構成が変わった可能性がある"
        )

    pagers = {}
    page_size_fields: tuple[tuple[str, str], ...] | None = None
    for form in parser.forms:
        if PAGE_SIZE_FIELD in form.selects:
            page_size_fields = tuple(form.fields)
        elif (number := form.value_of(PAGE_NUMBER_FIELD)) is not None:
            # ◀前 と 次▶ が同じ内容で 2 度ずつ出る (PC 用と携帯用)。後勝ちで構わない。
            pagers[number] = tuple(form.fields)

    return ListingPage(
        hit_count=hit_count,
        rows=rows,
        page_size_fields=page_size_fields,
        pager_fields=pagers,
    )
