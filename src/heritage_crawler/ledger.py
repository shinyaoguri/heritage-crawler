"""台帳取得層 — 分類 × 地域で検索し、CSV をキャッシュへ落とす (ADR 0002 の 1 段目)。

手順は 3 つ。順序も送る値も、2026-08-11 の実地検証で確かめたもの
(Issue #1 と #6 のコメント)。

1. ``GET /bsys/index`` で CSRF トークンを取る (Cookie とセットで有効)
2. ``POST /bsys/searchlist`` で分類と地域を指定して検索する
3. 応答 HTML の csv-list フォームの hidden 値を**そのまま** ``POST /utile/csv-list``

件数には 2 つの単位があり、混同すると欠損検査が狂う。

- 検索結果の件数表示 = **指定**単位 (既知の総数と突き合わせるのはこちら)
- CSV の行数 = **棟**単位 (102 は 1 指定あたり約 2.5 棟)

そのうえで、**件数表示どうしの引き算では網羅性を判定できない**。地域をまたぐ重複と
取りこぼしが相殺して消えるため (Issue #28)。判定には CSV を読んで数えたキーの
異なり数を使う (``summarize`` の ``unique_key_count``)。
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Callable, Iterator, Sequence
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


@dataclass(frozen=True)
class LedgerRow:
    """キャッシュ済み CSV の 1 行。列は名前で引く。

    列の**名前が同じでも中身の意味は分類によって変わる** (101 の
    ``重文指定年月日`` 列には登録年月日が入る)。値をそのまま使う前に、
    その分類で何を指す列かを確かめること (ADR 0008)。
    """

    category: Category
    values: tuple[str, ...]

    def get(self, column: str) -> str:
        return self.values[EXPECTED_CSV_HEADER.index(column)]

    @property
    def key(self) -> str:
        """``(台帳ID, 管理対象ID)``。詳細ページと結び付ける唯一のキー。"""
        return f"{self.get('台帳ID')}/{self.get('管理対象ID')}"


def read_ledger_rows(
    cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area] = SEARCH_AREAS,
) -> Iterator[LedgerRow]:
    """キャッシュ済みの台帳 CSV を分類 × 地域の順に読み、最後に回収ぶんを読む。

    重複は落とさない。同じ棟が複数の地域の CSV に現れることがあるため
    (102 の琵琶湖疏水施設が滋賀県と京都府の両方に出る)、必要な側で
    ``LedgerRow.key`` を使って落とす。

    回収 CSV (``listing.recover_missing``) は、地域では引けない指定を一覧から
    組み立て直したもの。地域別と同じ 18 列なので、読む側は区別しなくてよい。
    """
    for category in categories:
        paths = [cache.csv_path(category, area) for area in areas]
        paths.append(cache.recovered_csv_path(category))
        for path in paths:
            if not path.exists():
                continue
            for row in read_csv_rows(path.read_bytes()):
                if len(row) != len(EXPECTED_CSV_HEADER):
                    logger.warning("列数が合わない行を飛ばす (%s): %r", path, row)
                    continue
                yield LedgerRow(category=category, values=tuple(row))


def search_fields(csrf_token: str, category: Category, area_name: str) -> FormFields:
    """検索の POST パラメータ。

    分類の select の name は ``large_kind`` ではなく ``register_sub_id``。
    地域はコードではなく名前 (``北海道``) で送る。空文字なら全国が返る。
    seat_pref は格納値の完全一致で絞る (``北海道県`` や ``14`` では 0 件になる)。
    """
    return (
        ("_method", "POST"),
        ("_csrfToken", csrf_token),
        ("screen_id", "index"),
        ("page_no", "1"),
        ("register_sub_id", category.code),
        ("seat_pref", area_name),
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
    page = _search(fetcher, session, category, area.name)

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


def search[Page](
    fetcher: Fetcher,
    session: Session,
    category: Category,
    area_name: str,
    parse: Callable[[str], Page],
) -> Page:
    """検索して結果ページを読む。トークンが失効していたら 1 度だけ取り直す。

    同じ応答から読みたいものが 2 通りある — CSV 出力の hidden 値
    (``parse_search_page``) と結果一覧そのもの (``parse_listing_page``) —
    ので、読み方を渡してもらう。
    """
    for attempt in (1, 2):
        html = fetcher.post(
            SEARCH_URL, search_fields(session.token, category, area_name)
        ).decode("utf-8")
        try:
            return parse(html)
        except ParseError as error:
            if attempt == 2:
                raise
            logger.warning("検索応答を読めなかった (%s)。トークンを取り直して再試行する", error)
            session.refresh()
    raise AssertionError("到達しない")


def _search(fetcher: Fetcher, session: Session, category: Category, area_name: str) -> SearchPage:
    return search(fetcher, session, category, area_name, parse_search_page)


def fetch_whole_count(fetcher: Fetcher, session: Session, category: Category) -> int:
    """地域で絞らずに検索して、その分類の全国件数を得る。

    地域合計と突き合わせる基準はこれを使う。ソース側の件数が動いても同じ実行の
    中で取った値どうしを比べるので、固定値のように古びない。
    """
    count = _search(fetcher, session, category, "").hit_count
    logger.info("%s: 全国 %d 件 (指定)", category.code, count)
    return count


def fetch_ledgers(
    fetcher: Fetcher,
    cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area] = SEARCH_AREAS,
    *,
    force: bool = False,
    now: Callable[[], datetime] = _utc_now,
) -> None:
    """分類 × 地域を順に取得する。取得済みは飛ばす (``force`` で取り直す)。

    分類ごとに全国件数も取り直す。1 分類あたり 1 リクエストで、地域合計との
    差が取りこぼしと重複の両方を教えてくれる (``summarize``)。
    """
    session = Session(fetcher)
    for category in categories:
        cache.record_whole_count(category, fetch_whole_count(fetcher, session, category))
        for area in areas:
            if not force and cache.is_done(category, area):
                logger.debug("取得済みのため飛ばす: %s", entry_key(category, area))
                continue
            entry, raw = fetch_one(fetcher, session, category, area, now=now)
            cache.record(category, area, entry, raw)


@dataclass(frozen=True)
class CategorySummary:
    """1 分類ぶんの取得結果と、その網羅性。"""

    category: Category
    fetched_areas: int
    total_areas: int
    area_hit_count: int
    """地域ごとの件数表示の合計 (指定単位)。地域をまたぐ指定は二重に数えられる。"""

    whole_count: int | None
    """地域で絞らずに数えた全国件数 (指定単位)。未取得なら None。"""

    row_count: int
    """CSV 行数の合計 (棟単位)。指定単位とは比べない。"""

    unique_key_count: int
    """CSV に実際に現れた ``(台帳ID, 管理対象ID)`` の異なり数 (棟単位)。

    件数表示ではなく中身を数えたもの。重複を除いた実数がこれで分かる。
    """

    @property
    def is_complete(self) -> bool:
        return self.fetched_areas == self.total_areas

    @property
    def difference(self) -> int | None:
        """地域合計 − 全国件数。**重複と取りこぼしが相殺して隠れる** (Issue #28)。

        負 = どの地域でも引けない指定がある (取りこぼし)。
        正 = 複数の地域に現れる指定がある (統合時に (台帳ID, 管理対象ID) で排除する)。

        どちらも件数表示どうしの引き算なので、重複 34 と取りこぼし 1 が同時にあれば
        「重複 33」としか出ない。棟に展開されない分類では ``missing_count`` を見る。
        """
        if self.whole_count is None:
            return None
        return self.area_hit_count - self.whole_count

    @property
    def duplicate_rows(self) -> int:
        """複数の地域の CSV に現れた行数。統合時に落ちるぶんで、異常ではない。"""
        return self.row_count - self.unique_key_count

    @property
    def missing_count(self) -> int | None:
        """全国件数 − 手元の異なり数。**重複と相殺しない取りこぼしの数** (Issue #28)。

        正 = どの地域でも引けない指定がある。負 = 全国件数の数え方が指定単位で
        ないか、同じ管理対象を共有する 2 指定を 2 と数えている。

        棟に展開される分類 (102) では異なり数が棟単位で全国件数と単位が違うため
        None を返す。そちらは ``difference`` でしか見られない。
        """
        if self.whole_count is None or self.category.expands_to_buildings:
            return None
        return self.whole_count - self.unique_key_count

    @property
    def note(self) -> str:
        """行末に出す気付き。気になるところが無ければ空。

        **取りこぼしとは限らない** — 地域をまたぐ重複も報せる。取りこぼしの
        兆候だけを見たいときは ``looks_complete`` を使う。
        """
        if not self.is_complete:
            return f"未取得 {self.total_areas - self.fetched_areas} 地域"
        if (missing := self.missing_count) is not None:
            # 中身の異なり数と比べているので、重複があっても取りこぼしが隠れない。
            if missing > 0:
                return f"どの地域でも引けない {missing:,} 件"
            if missing < 0:
                return f"全国件数より {-missing:,} 件多い (件数表示の数え方を疑う)"
        elif self.difference is not None:
            # 棟に展開される分類は異なり数 (棟) と全国件数 (指定) の単位が違う。
            # 件数表示どうしの差しか見られず、重複と取りこぼしは相殺しうる (Issue #28)。
            if self.difference < 0:
                return f"どの地域でも引けない {-self.difference:,} 件"
            if self.difference:
                return f"地域をまたぐ重複 {self.difference:,} 件"
        return ""

    @property
    def looks_complete(self) -> bool:
        """取りこぼしの兆候が無いか。

        **偽のときに消してはいけない** — 台帳から消えた指定を「指定解除」と
        断じてよいのは、その分類が丸ごと取れていると言えるときだけ (ADR 0018)。

        判定の根拠は分類で違う (棟に展開される 102 だけは件数表示どうしの差しか
        見られない)。**地域をまたぐ重複は取りこぼしではない**ので、正の差は
        通す。全国件数が無ければ突き合わせられないので偽にする。
        """
        if not self.is_complete or self.whole_count is None:
            return False
        if (missing := self.missing_count) is not None:
            # 負 = 全国件数より手元が多い。数え方を疑う状態なので通さない。
            return missing == 0
        return self.difference is not None and self.difference >= 0


def summarize(
    cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area] = SEARCH_AREAS,
) -> list[CategorySummary]:
    """マニフェストの件数に加え、CSV の中身を読んでキーの異なり数も数える。

    件数表示だけでは重複と取りこぼしが相殺して見えないため (Issue #28)、
    全 CSV (2 万行台) を読み直す。数秒かかるが、報告のたびにしか走らない。
    """
    summaries = []
    for category in categories:
        entries = [
            entry
            for area in areas
            if (entry := cache.entries.get(entry_key(category, area))) is not None
        ]
        keys = {row.key for row in read_ledger_rows(cache, [category], areas)}
        summaries.append(
            CategorySummary(
                category=category,
                fetched_areas=len(entries),
                total_areas=len(areas),
                area_hit_count=sum(entry.hit_count for entry in entries),
                whole_count=cache.whole_counts.get(category.code),
                row_count=sum(entry.row_count for entry in entries),
                unique_key_count=len(keys),
            )
        )
    return summaries


def format_summary(summaries: Sequence[CategorySummary]) -> str:
    """取得結果を人が読める形にする。網羅できていなければ行末で知らせる。"""
    lines = ["分類  地域      全国   地域合計        棟      異なり", "-" * 72]
    notes = []
    for summary in summaries:
        whole = f"{summary.whole_count:,}" if summary.whole_count is not None else "-"
        note = summary.note
        lines.append(
            f"{summary.category.code}  "
            f"{summary.fetched_areas:>2}/{summary.total_areas:<2}  "
            f"{whole:>8}  {summary.area_hit_count:>8,}  {summary.row_count:>8,}  "
            f"{summary.unique_key_count:>8,}" + (f"  ← {note}" if note else "")
        )
        known = summary.category.known_designation_count
        if summary.whole_count is not None and summary.whole_count != known:
            notes.append(
                f"※ {summary.category.code} の全国件数が 2026-08-11 の実測 "
                f"({known:,}) から {summary.whole_count - known:+,} 件変わっている"
            )
    lines.append("(全国・地域合計は指定単位、棟は CSV の行数、異なりは棟のキーの異なり数)")
    return "\n".join(lines + notes)
