"""検索結果一覧で台帳の網羅性を確かめ、引けない指定を回収する (ADR 0017)。

台帳 (CSV) は分類 × 地域で取る。地域は ``seat_pref`` の**完全一致**で絞るので、
データ側の都道府県が空の指定は**どの地域でも引けない**。空を送ると全国検索に
なるため、未正規化の値を直接送る手 (``catalog.IRREGULAR_AREAS``) も使えない。
そして全国 CSV は 504 で取れない (Issue #28)。

一覧は地域で絞らずに全ページ辿れて、1 行 = 1 **指定**として
``(台帳ID, 管理対象ID)``・名称・地域欄・緯度経度を持つ。CSV が下流へ渡している
のは緯度経度だけなので、一覧の行から台帳の行を組み立て直せる。

**棟に展開される分類 (102) は対象外。** 一覧の行は指定単位、CSV の行は棟単位で、
複数棟の指定は詳細ページへ直接リンクされない (棟一覧のモーダルになる)。

リクエスト数は ``ceil(指定件数 / 100) + 2``。全分類でも 200 弱で、台帳取得 (204)
と同じ桁に収まる。
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from math import ceil
from typing import Final

from heritage_crawler.cache import LedgerCache, atomic_write
from heritage_crawler.catalog import SEARCH_AREAS, Area, Category
from heritage_crawler.http import Fetcher
from heritage_crawler.ledger import (
    AREA_COLUMN_INDEX,
    EXPECTED_CSV_HEADER,
    SEARCH_URL,
    Session,
    read_ledger_rows,
    search,
)
from heritage_crawler.search_page import (
    MAX_PAGE_SIZE,
    PAGE_SIZE_FIELD,
    ListingPage,
    ListingRow,
    ParseError,
    parse_listing_page,
)

logger = logging.getLogger(__name__)

MAX_EXTRA_PAGES: Final = 2
"""想定ページ数からこれだけ余分に進んだら打ち切る。

最終ページには次ページのフォームが出ないので普通は自然に止まる。止まらないのは
同じページが返り続けているときで、そのまま回し続けると相手に無駄な負荷をかける。
"""

MAX_LISTED: Final = 20
"""報告に名前を並べる上限。超えたぶんは件数だけ出す (取りこぼしは普通 1 桁)。"""


class ListingError(RuntimeError):
    """一覧の取得が期待どおりに進まなかった。"""


@dataclass(frozen=True)
class Listing:
    """1 分類ぶんの一覧の収集結果。"""

    category: Category
    hit_count: int
    """件数表示の総数 (指定単位)。集めた行数と一致するはず。"""

    rows: tuple[ListingRow, ...]
    """集めた行。同じキーは最初の 1 つだけ残す。"""

    duplicate_keys: tuple[str, ...]
    """2 度以上現れたキー。ページ送りが空回りしていれば大量に出る。"""

    @property
    def is_consistent(self) -> bool:
        return len(self.rows) == self.hit_count and not self.duplicate_keys


def fetch_listing(fetcher: Fetcher, session: Session, category: Category) -> Listing:
    """地域で絞らずに検索し、一覧を最後のページまで辿ってキーを集める。"""
    if category.expands_to_buildings:
        raise ListingError(
            f"{category.code} は 1 指定が複数の棟に展開されるため、一覧のキーは台帳と"
            "突き合わせられない (一覧は指定単位、CSV は棟単位)"
        )

    page = _widen(fetcher, search(fetcher, session, category, "", parse_listing_page))
    limit = ceil(page.hit_count / max(len(page.rows), 1)) + MAX_EXTRA_PAGES

    rows: dict[str, ListingRow] = {}
    duplicates: list[str] = []
    number = 1
    while True:
        for row in page.rows:
            if row.key in rows:
                duplicates.append(row.key)
            else:
                rows[row.key] = row
        logger.info(
            "%s: %d ページ目まで %d 件 (全 %d 件)", category.code, number, len(rows), page.hit_count
        )
        fields = page.fields_for_page(number + 1)
        if fields is None:
            break
        if number >= limit:
            raise ListingError(
                f"{category.code} の一覧が {number} ページを超えても終わらない。"
                "ページ送りが空回りしている可能性がある"
            )
        page = parse_listing_page(fetcher.post(SEARCH_URL, fields).decode("utf-8"))
        number += 1

    return Listing(
        category=category,
        hit_count=page.hit_count,
        rows=tuple(rows.values()),
        duplicate_keys=tuple(duplicates),
    )


def _widen(fetcher: Fetcher, page: ListingPage) -> ListingPage:
    """1 ページの表示件数を最大にする。ページ数が 1/5 になる。

    送る値は表示件数フォームの hidden をそのまま使い、``pageSize`` だけ足す
    (ADR 0002 の「推測して組み立てない」)。
    """
    if page.page_size_fields is None:
        logger.warning("表示件数フォームが無い。既定の表示件数のまま辿る")
        return page
    fields = [*page.page_size_fields, (PAGE_SIZE_FIELD, MAX_PAGE_SIZE)]
    widened = parse_listing_page(fetcher.post(SEARCH_URL, fields).decode("utf-8"))
    if not widened.rows:
        raise ParseError("表示件数を変えたら一覧が空になった")
    return widened


@dataclass(frozen=True)
class ListingAudit:
    """一覧と手元の台帳の突き合わせ。"""

    listing: Listing
    ledger_key_count: int

    missing: tuple[ListingRow, ...]
    """一覧にあって台帳に無い = 取りこぼし。**1 件ずつ名指しできる**のが要点。"""

    unexpected: tuple[str, ...]
    """台帳にあって一覧に無いキー。指定解除の直後などに起こりうる。"""

    @property
    def category(self) -> Category:
        return self.listing.category


def audit_listing(
    cache: LedgerCache,
    listing: Listing,
    areas: Sequence[Area] = SEARCH_AREAS,
) -> ListingAudit:
    """収集した一覧とキャッシュ済み CSV のキー集合を突き合わせる。"""
    ledger_keys = {row.key for row in read_ledger_rows(cache, [listing.category], areas)}
    listing_keys = {row.key for row in listing.rows}
    return ListingAudit(
        listing=listing,
        ledger_key_count=len(ledger_keys),
        missing=tuple(row for row in listing.rows if row.key not in ledger_keys),
        unexpected=tuple(sorted(ledger_keys - listing_keys)),
    )


def recover_missing(cache: LedgerCache, audit: ListingAudit) -> int:
    """取りこぼした指定を、一覧の行から台帳の CSV として書き直す。

    埋めるのは名称・地域欄・緯度経度だけ。**CSV から採るのは緯度経度だけ**
    (残りは詳細ページから採る。ADR 0008) なので、これで ``fetch-detail`` も
    ``build-records`` も通常の行と同じように流れる。

    毎回まるごと書き直す。元データの都道府県が埋まれば地域別 CSV で引けるように
    なり、この回収ぶんは消えるべきものだから (追記すると古い行が残り続ける)。
    """
    path = cache.recovered_csv_path(audit.category)
    if not audit.missing:
        path.unlink(missing_ok=True)
        return 0

    rows = [_csv_row(row) for row in sorted(audit.missing, key=lambda row: row.key)]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(EXPECTED_CSV_HEADER)
    writer.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 地域別 CSV と同じく UTF-8 (BOM 付き)。読む側が分岐せずに済む。
    atomic_write(path, buffer.getvalue().encode("utf-8-sig"))
    logger.info("%s: 取りこぼし %d 件を %s へ回収した", audit.category.code, len(rows), path)
    return len(rows)


def _csv_row(row: ListingRow) -> list[str]:
    """一覧の行を台帳 CSV の 18 列に写す。埋められない列は空のまま。"""
    values = dict.fromkeys(EXPECTED_CSV_HEADER, "")
    # 一覧が持つのは分類コードで、台帳ID ではない (#74)。現行 4 分類では同値なので
    # このまま書ける。台帳ID が食い違う分類を足すときは、分類側から台帳ID を採る。
    values["台帳ID"] = row.category_code
    values["管理対象ID"] = row.kanri_taishou_id
    values["名称"] = row.name
    # 地域の列は見出しが分類で変わるので位置で指す (#74)。回収 CSV は既定の
    # 見出しで書き出すが、意味は「その分類の地域欄」で、都道府県とは限らない。
    values[EXPECTED_CSV_HEADER[AREA_COLUMN_INDEX]] = row.area
    values["緯度"] = row.latitude
    values["経度"] = row.longitude
    return [values[column] for column in EXPECTED_CSV_HEADER]


def format_audits(audits: Sequence[ListingAudit]) -> str:
    """人が読める形にする。取りこぼしはキーと名称と地域欄まで出す。"""
    lines = ["分類    全国    一覧    台帳  取りこぼし    余り", "-" * 52]
    notes: list[str] = []
    for audit in audits:
        listing = audit.listing
        lines.append(
            f"{audit.category.code}  {listing.hit_count:>6,}  {len(listing.rows):>6,}  "
            f"{audit.ledger_key_count:>6,}  {len(audit.missing):>10,}  {len(audit.unexpected):>6,}"
        )
        if not listing.is_consistent:
            notes.append(
                f"※ {audit.category.code} の一覧が件数表示と合わない "
                f"(集めた {len(listing.rows):,} 件 / 表示 {listing.hit_count:,} 件 "
                f"/ 重複 {len(listing.duplicate_keys):,} 件)"
            )
        notes.extend(_missing_notes(audit))
        if audit.unexpected:
            notes.append(
                f"※ {audit.category.code} の台帳に一覧から消えたキーが "
                f"{len(audit.unexpected):,} 件ある (指定解除を疑う): "
                + ", ".join(audit.unexpected[:MAX_LISTED])
            )
    lines.append("(全国は件数表示、一覧は集めた行、台帳は CSV のキーの異なり数。いずれも指定単位)")
    return "\n".join(lines + notes)


def _missing_notes(audit: ListingAudit) -> list[str]:
    if not audit.missing:
        return []
    notes = [f"※ {audit.category.code} の取りこぼし {len(audit.missing):,} 件:"]
    for row in audit.missing[:MAX_LISTED]:
        # 地域欄が空なら理由まで分かる (seat_pref は完全一致なのでどの値でも引けない)。
        reason = "地域欄が空" if not row.area else f"地域欄は「{row.area}」"
        if not row.latitude:
            reason += "・緯度経度も無い"
        notes.append(f"   {row.key}  {row.name}  ({reason})")
    if len(audit.missing) > MAX_LISTED:
        notes.append(f"   ... 他 {len(audit.missing) - MAX_LISTED:,} 件")
    return notes
