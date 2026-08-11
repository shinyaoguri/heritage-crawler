"""台帳取得層 — 分類 × 地域で検索し、CSV をキャッシュへ落とす (ADR 0002 の 1 段目)。

手順は 3 つ。順序も送る値も、2026-08-11 の実地検証で確かめたもの
(Issue #1 と #6 のコメント)。

1. ``GET /bsys/index`` で CSRF トークンを取る (Cookie とセットで有効)
2. ``POST /bsys/searchlist`` で分類と地域を指定して検索する
3. 応答 HTML の csv-list フォームの hidden 値を**そのまま** ``POST /utile/csv-list``

件数には 2 つの単位があり、混同すると欠損検査が狂う。

- 検索結果の件数表示 = **指定**単位 (既知の総数と突き合わせるのはこちら)
- CSV の行数 = **棟**単位 (102 は 1 指定あたり約 2.5 棟)
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from heritage_crawler.cache import LedgerCache, LedgerEntry, entry_key
from heritage_crawler.catalog import BASE_URL, SEARCH_AREAS, Area, Category
from heritage_crawler.http import Fetcher, FormFields
from heritage_crawler.search_page import (
    ParseError,
    SearchPage,
    extract_csrf_token,
    parse_search_page,
)

logger = logging.getLogger(__name__)

INDEX_URL: Final = f"{BASE_URL}/bsys/index"
SEARCH_URL: Final = f"{BASE_URL}/bsys/searchlist"
CSV_URL: Final = f"{BASE_URL}/utile/csv-list"

EXPECTED_CSV_HEADER: Final[tuple[str, ...]] = (
    "台帳ID",
    "管理対象ID",
    "名称",
    "棟名",
    "文化財種類",
    "種別1",
    "種別2",
    "国",
    "時代",
    "重文指定年月日",
    "国宝指定年月日",
    "都道府県",
    "所在地",
    "保管施設の名称",
    "所有者名",
    "管理団体又は責任者",
    "緯度",
    "経度",
)


class LedgerError(RuntimeError):
    """台帳の取得結果が期待した形をしていない。"""


def search_fields(csrf_token: str, category: Category, area: Area) -> FormFields:
    """検索の POST パラメータ。

    分類の select の name は ``large_kind`` ではなく ``register_sub_id``。
    地域はコードではなく名前 (``北海道``) で送る。
    """
    return (
        ("_method", "POST"),
        ("_csrfToken", csrf_token),
        ("screen_id", "index"),
        ("page_no", "1"),
        ("register_sub_id", category.code),
        ("seat_pref", area.name),
    )


def read_csv_rows(raw: bytes) -> list[list[str]]:
    """CSV の本体行を読む。ヘッダは検証だけして返さない。

    文字コードは UTF-8 (BOM 付き)。Shift_JIS ではない。
    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise LedgerError("CSV が UTF-8 として読めない。応答が CSV でない可能性がある") from error

    rows = [row for row in csv.reader(io.StringIO(text)) if row]
    if not rows:
        raise LedgerError("CSV が空だった")
    header = tuple(rows[0])
    if header != EXPECTED_CSV_HEADER:
        raise LedgerError(
            "CSV のヘッダが既知の 18 列と一致しない。"
            "csv-list へ送る hidden 値が応答 HTML から取り出したものになっているか"
            "(推測して組み立てると 504 になる)、サイトの列構成が変わっていないかを疑う。"
            f" 実際のヘッダ: {header}"
        )
    return rows[1:]


class Session:
    """CSRF トークンの取得と取り直しをまとめる。"""

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher
        self._token: str | None = None

    @property
    def token(self) -> str:
        if self._token is None:
            return self.refresh()
        return self._token

    def refresh(self) -> str:
        logger.info("検索トップページから CSRF トークンを取得する")
        self._token = extract_csrf_token(self._fetcher.get(INDEX_URL).decode("utf-8"))
        return self._token


def _utc_now() -> datetime:
    return datetime.now(UTC)


def fetch_one(
    fetcher: Fetcher,
    session: Session,
    category: Category,
    area: Area,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> tuple[LedgerEntry, bytes]:
    """1 つの (分類 × 地域) を取得する。0 件なら CSV は要求せず空を返す。"""
    page = _search(fetcher, session, category, area)

    if page.csv_fields is None:
        logger.info("%s × %s: 0 件", category.code, area.name)
        raw = b""
        row_count = 0
    else:
        raw = fetcher.post(CSV_URL, page.csv_fields)
        row_count = len(read_csv_rows(raw))
        logger.info(
            "%s × %s: %d 件 (指定) / %d 行 (棟) / %d bytes",
            category.code,
            area.name,
            page.hit_count,
            row_count,
            len(raw),
        )

    entry = LedgerEntry(
        category_code=category.code,
        area_name=area.name,
        hit_count=page.hit_count,
        row_count=row_count,
        byte_count=len(raw),
        fetched_at=now().isoformat(timespec="seconds"),
    )
    return entry, raw


def _search(fetcher: Fetcher, session: Session, category: Category, area: Area) -> SearchPage:
    """検索して結果ページを読む。トークンが失効していたら 1 度だけ取り直す。"""
    html = fetcher.post(SEARCH_URL, search_fields(session.token, category, area)).decode("utf-8")
    try:
        return parse_search_page(html)
    except ParseError as error:
        logger.warning("検索応答を読めなかった (%s)。トークンを取り直して再試行する", error)
        session.refresh()
        html = fetcher.post(SEARCH_URL, search_fields(session.token, category, area)).decode(
            "utf-8"
        )
        return parse_search_page(html)


def fetch_ledgers(
    fetcher: Fetcher,
    cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area] = SEARCH_AREAS,
    *,
    force: bool = False,
    now: Callable[[], datetime] = _utc_now,
) -> None:
    """分類 × 地域を順に取得する。取得済みは飛ばす (``force`` で取り直す)。"""
    session = Session(fetcher)
    targets = [(category, area) for category in categories for area in areas]
    for index, (category, area) in enumerate(targets, start=1):
        if not force and cache.is_done(category, area):
            logger.debug("取得済みのため飛ばす: %s", entry_key(category, area))
            continue
        logger.debug("(%d/%d) %s", index, len(targets), entry_key(category, area))
        entry, raw = fetch_one(fetcher, session, category, area, now=now)
        cache.record(category, area, entry, raw)


@dataclass(frozen=True)
class CategorySummary:
    """1 分類ぶんの取得結果。既知の指定件数との突き合わせに使う。"""

    category: Category
    fetched_areas: int
    total_areas: int
    hit_count: int
    """件数表示の合計 (指定単位)。"""

    row_count: int
    """CSV 行数の合計 (棟単位)。"""

    @property
    def is_complete(self) -> bool:
        return self.fetched_areas == self.total_areas

    @property
    def difference(self) -> int:
        """既知の指定件数との差。負なら取りこぼし、正なら新規指定などの増加。"""
        return self.hit_count - self.category.known_designation_count


def summarize(
    cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area] = SEARCH_AREAS,
) -> list[CategorySummary]:
    summaries = []
    for category in categories:
        entries = [
            entry
            for area in areas
            if (entry := cache.entries.get(entry_key(category, area))) is not None
        ]
        summaries.append(
            CategorySummary(
                category=category,
                fetched_areas=len(entries),
                total_areas=len(areas),
                hit_count=sum(entry.hit_count for entry in entries),
                row_count=sum(entry.row_count for entry in entries),
            )
        )
    return summaries


def format_summary(summaries: Sequence[CategorySummary]) -> str:
    """取得結果を人が読める形にする。欠損があれば行末で知らせる。

    件数表示の合計は指定単位のため既知の総数と比べられる。CSV 行数は棟単位で
    単位が違うので、比較には使わない。
    """
    lines = ["分類  地域      指定 (既知)          棟", "-" * 52]
    for summary in summaries:
        note = ""
        if not summary.is_complete:
            note = f"  ← 未取得 {summary.total_areas - summary.fetched_areas} 地域"
        elif summary.difference < 0:
            note = f"  ← 既知より {-summary.difference:,} 件少ない"
        elif summary.difference > 0:
            note = f"  ← 既知より {summary.difference:,} 件多い"
        lines.append(
            f"{summary.category.code}  "
            f"{summary.fetched_areas:>2}/{summary.total_areas:<2}  "
            f"{summary.hit_count:>7,} ({summary.category.known_designation_count:>6,})  "
            f"{summary.row_count:>7,}{note}"
        )
    return "\n".join(lines)
