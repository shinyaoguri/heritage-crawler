"""詳細取得層 — 台帳の各行から詳細ページを巡回して生 HTML を貯める
(ADR 0002 の 2 段目)。

パースはしない。取得と解析を分けておかないと、パース仕様を変えるたびに
2 万ページを取り直すことになる (ADR 0006)。

台帳と違い、詳細ページはセッションも CSRF も要らない静的な GET なので、
取得の単位は ``(分類コード, 管理対象ID)`` だけで完結する。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Final

from heritage_crawler.cache import (
    ISSUE_TRUNCATED,
    DetailCache,
    DetailEntry,
    LedgerCache,
    detail_key,
)
from heritage_crawler.catalog import Area, Category, detail_url
from heritage_crawler.http import Fetcher, FetchError
from heritage_crawler.ledger import read_ledger_rows

logger = logging.getLogger(__name__)

PROGRESS_EVERY: Final = 100
CONSECUTIVE_FAILURE_LIMIT: Final = 10
"""これだけ続けて失敗したら打ち切る。

相手が落ちているか、こちらが弾かれている。気付かずに 2 万回叩き続けない。
"""

ERROR_PAGE_MARKERS: Final = ("必要な情報が足りません",)
"""**HTTP 200 で返ってくるエラーページ**の目印 (ADR 0011)。

4 req/s で叩いたときに応答の 15% がこれになった。ステータスは 200 なので、
本文を見ないと失敗と分からない。見ないままキャッシュすると、取得は成功した
ことになり、連続失敗の打ち切りも働かない。
"""

MIN_DETAIL_BYTES: Final = 5_000
"""詳細ページとして受け取る下限。

実物は 33〜55 KB で、上記のエラーページは 3 KB 弱。目印が変わっても大きさで気付く。
"""


TRUNCATED_MARKERS: Final = ("<!-- heritage_detail END -->", "/bsys/error")
"""**途中まで描かれた詳細ページ**の目印 (#107 / ADR 0030)。両方そろったら当たり。

相手のサーバは、ページを組み立てている途中で落ちると、そこまで書き出した HTML の
後ろにエラー画面 (``location.href='/bsys/error'``) をつないで HTTP 500 で返す。
``901/00000024`` (佐渡島の金山) では、主情報と本文側の解説文を書き終えた後、
「構成資産」の一覧を描くところで落ちていた (2026-09-23 実測)。

前者が主情報の終わりで、ここまで描かれていれば名称・所在地などは揃っている。
存在しない ID の 500 はエラー画面だけなので当たらない。
"""


def truncated(html: bytes) -> bool:
    """相手が詳細ページを途中まで描いて落ちたか (ADR 0030)。

    **エラー応答の本文にだけ当てる。** 正常な詳細ページも前者の目印は持つ。

    >>> truncated(b"...<!-- heritage_detail END -->...location.href='/bsys/error';")
    True
    >>> truncated(b"<script>location.href='/bsys/error';</script>")
    False
    """
    text = html.decode("utf-8", "replace")
    return all(marker in text for marker in TRUNCATED_MARKERS)


def not_found(html: bytes) -> str:
    """相手が「そのレコードは無い」と答えたか。答えていれば目印を返す。

    **この文言は 2 つのことを意味する** (ADR 0011 / ADR 0021)。存在しない
    管理対象ID を引いたときにも返るが、**4 req/s で叩いたときにも応答の 15% が
    これになった**。「レコードが無い」と「いま答えられない」の区別は本文からは
    付かないので、削除の判定に使うときは対照群が要る (``probe_presence``)。

    >>> not_found("必要な情報が足りません".encode())
    '必要な情報が足りません'
    >>> not_found(b"x" * 40_000)
    ''
    """
    text = html.decode("utf-8", "replace")
    return next((marker for marker in ERROR_PAGE_MARKERS if marker in text), "")


def rejected(html: bytes) -> str:
    """詳細ページとして受け取ってよいか。駄目なら理由を返す (ADR 0011)。

    **パースはしない。** 取得層が見るのは「相手がエラーページを返していないか」
    だけで、項目の読み取りは解析層の仕事 (ADR 0006 の分離を保つ)。

    >>> rejected(b"x" * 40_000)
    ''
    >>> rejected("必要な情報が足りません".encode())
    'エラーページが返ってきた (必要な情報が足りません)'
    >>> rejected(b"<html></html>")
    '詳細ページとして小さすぎる (13 バイト)'
    """
    if marker := not_found(html):
        return f"エラーページが返ってきた ({marker})"
    if len(html) < MIN_DETAIL_BYTES:
        return f"詳細ページとして小さすぎる ({len(html):,} バイト)"
    return ""


class Presence(Enum):
    """削除候補を詳細ページに問い合わせた結果 (ADR 0021)。"""

    GONE = "gone"
    """「必要な情報が足りません」が返り、そのとき相手は正常に答えていた。"""

    ALIVE = "alive"
    """詳細ページが返った。台帳から外れただけで、データベースにはまだ居る。"""

    UNKNOWN = "unknown"
    """確かめられなかった。**落とさない** — 翌週やり直す。"""


def probe_presence(
    fetcher: Fetcher, targets: Sequence[Target], control: Target
) -> dict[str, Presence]:
    """削除候補が本当にデータベースから消えているかを 1 件ずつ確かめる (ADR 0021)。

    台帳からの不在は*消極的*な証拠でしかない。詳細ページを引けば「そのレコードは
    無い」という**積極的な返答**を得られる — とくに 102 は全国件数とキーの異なり数を
    直接比べられないので (棟単位)、これが唯一の個別証拠になる。

    **対照群が要。** 「必要な情報が足りません」は過負荷のときにも返る文言なので
    (``not_found``)、実在すると分かっている ``control`` を**検査の前後に 1 件ずつ**
    引く。両方が正常な詳細ページを返したときだけ「無い」を信じる。前だけでは
    検査中に混み始めた回を取りこぼす。

    **キャッシュには書かない。** ``fetch_details`` を流用すると、消えた行が
    「取得に失敗した」としてマニフェストに積まれ、``--retry-failed`` が毎週それを
    叩き、連続失敗の打ち切り (``CONSECUTIVE_FAILURE_LIMIT``) が誤発動する。
    """
    if not targets:
        return {}
    if not _answers_normally(fetcher, control):
        logger.warning(
            "対照群 %s が正常な詳細ページを返さない。%d 件の存在を確かめずに進む",
            control.key,
            len(targets),
        )
        return {target.key: Presence.UNKNOWN for target in targets}

    found: dict[str, Presence] = {}
    for target in targets:
        try:
            html = fetcher.get(target.url)
        except FetchError as error:
            logger.warning("%s の存在を確かめられない: %s", target.key, error)
            found[target.key] = Presence.UNKNOWN
            continue
        if not_found(html):
            found[target.key] = Presence.GONE
        elif rejected(html):
            # 目印は無いが詳細ページでもない。断じられないので分からないままにする。
            found[target.key] = Presence.UNKNOWN
        else:
            found[target.key] = Presence.ALIVE

    if Presence.GONE in found.values() and not _answers_normally(fetcher, control):
        # 検査中に相手が混み始めた回。「無い」という返答の時制を担保できない。
        logger.warning("検査の後で対照群 %s が崩れた。「無い」という結論を取り下げる", control.key)
        found = {
            key: Presence.UNKNOWN if value is Presence.GONE else value
            for key, value in found.items()
        }
    logger.info(
        "存在を確かめた: 無い %d 件 / 生きている %d 件 / 分からない %d 件",
        *(sum(1 for value in found.values() if value is state) for state in Presence),
    )
    return found


def _answers_normally(fetcher: Fetcher, control: Target) -> bool:
    """相手がいま正常に答えているか。実在すると分かっているキーで測る。"""
    try:
        return not rejected(fetcher.get(control.url))
    except FetchError as error:
        logger.warning("対照群 %s を引けない: %s", control.key, error)
        return False


class DetailError(RuntimeError):
    """詳細ページの巡回を続けられない。"""


@dataclass(frozen=True)
class Target:
    """詳細ページ 1 件の取得対象。

    ID は必ず文字列のまま扱う。管理対象ID には短い連番 (``23``) と 8 桁ゼロ詰め
    (``00003904``) が混在し、数値に変換するとゼロ詰めが落ちて到達できなくなる。
    """

    category_code: str
    """**CSV の台帳ID ではなく分類コード。**

    詳細ページ URL の第 1 セグメントもキャッシュの置き場もこちらで決まる
    (``catalog.detail_url``)。台帳ID は 1 つに複数の分類が同居するので
    (401 に 401 / 411 / 412)、取りに行った先を表せない。
    """

    kanri_taishou_id: str
    name: str
    """ログに出すためだけの表示名。取得には使わない。"""

    @property
    def key(self) -> str:
        return detail_key(self.category_code, self.kanri_taishou_id)

    @property
    def url(self) -> str:
        return detail_url(self.category_code, self.kanri_taishou_id)


def read_targets(
    cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area] | None = None,
) -> list[Target]:
    """キャッシュ済みの台帳 CSV から取得対象を作る。

    ``(分類コード, 管理対象ID)`` で重複を落とす。複数の都道府県に現れる指定がある
    ため (102 の琵琶湖疏水施設が滋賀県と京都府の両方に出る)。棟レベルでキーが
    完全に重なるので、ここで落とせば詳細ページを二度取りに行かずに済む。

    **分類コードは CSV の台帳ID 列ではなく、その CSV を取りに行った分類から採る。**
    台帳ID には複数の分類が同居しており (401 に 401 / 411 / 412)、そのままでは
    詳細ページに到達できない (``catalog.detail_url``)。
    """
    targets: dict[str, Target] = {}
    for row in read_ledger_rows(cache, categories, areas):
        target = Target(
            category_code=row.category.code,
            kanri_taishou_id=row.get("管理対象ID"),
            name=" ".join(part for part in (row.get("名称"), row.get("棟名")) if part),
        )
        targets.setdefault(target.key, target)
    return list(targets.values())


@dataclass
class DetailRun:
    """1 回の実行で何が起きたか。"""

    total: int
    """引き渡された対象の数。"""

    skipped: int = 0
    """取得済みとして飛ばした数。"""

    fetched: int = 0
    failed: int = 0
    truncated: int = 0
    """途中まで描かれたページを保存した数 (ADR 0030)。``fetched`` にも含む。"""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def recheck_cache(
    cache: DetailCache, targets: Sequence[Target], now: Callable[[], datetime] = _utc_now
) -> list[Target]:
    """キャッシュ済みの生 HTML を検査し、エラーページだったものを未取得へ戻す。

    200 で返るエラーページに気付く前に取ったぶんは「取得済み」として残っている
    (ADR 0011 の事故)。取り直すには、まず取得済みの印を外す必要がある。
    通信はしない。
    """
    found = []
    for target in targets:
        if not cache.is_done(target.category_code, target.kanri_taishou_id):
            continue
        try:
            reason = rejected(cache.read_html(target.category_code, target.kanri_taishou_id))
        except OSError as error:
            reason = f"キャッシュを読めない: {error}"
        if not reason:
            continue
        cache.record(
            DetailEntry(
                category_code=target.category_code,
                kanri_taishou_id=target.kanri_taishou_id,
                ok=False,
                byte_count=0,
                fetched_at=now().isoformat(timespec="seconds"),
                error=reason,
            )
        )
        found.append(target)
    logger.info("キャッシュを検査した: 取り直しが要るもの %d 件", len(found))
    return found


def fetch_details(
    fetchers: Sequence[Fetcher],
    cache: DetailCache,
    targets: Sequence[Target],
    *,
    force: bool = False,
    now: Callable[[], datetime] = _utc_now,
    clock: Callable[[], float] = time.monotonic,
    progress_every: int = PROGRESS_EVERY,
    failure_limit: int = CONSECUTIVE_FAILURE_LIMIT,
) -> DetailRun:
    """詳細ページを順に取得して生 HTML をキャッシュへ落とす。

    並列度は ``fetchers`` の数で決まる (既定は 1 本 = 逐次)。相手から見たレートは
    クライアント側で共有する ``RateLimiter`` が抑えるので、ここでは数だけを見る。

    1 件の失敗では止まらない。2 万件のうち 1 件が 404 を返しただけで全体が
    終わってしまっては、再開のたびに同じところで止まる。記録して次へ進み、
    後から ``failures`` を拾い直す。
    """
    if not fetchers:
        raise ValueError("fetchers は 1 つ以上要る")

    pending = [
        target
        for target in targets
        if force or not cache.is_done(target.category_code, target.kanri_taishou_id)
    ]
    run = DetailRun(total=len(targets), skipped=len(targets) - len(pending))
    logger.info("取得対象 %d 件 (取得済みを飛ばして %d 件)", run.total, len(pending))
    if not pending:
        return run

    state = _RunState(
        cache=cache,
        run=run,
        remaining=len(pending),
        now=now,
        clock=clock,
        progress_every=progress_every,
        failure_limit=failure_limit,
    )
    idle: queue.SimpleQueue[Fetcher] = queue.SimpleQueue()
    for fetcher in fetchers:
        idle.put(fetcher)

    def visit(target: Target) -> None:
        if state.stopped:
            return
        fetcher = idle.get()
        try:
            state.handle(fetcher, target)
        finally:
            idle.put(fetcher)

    try:
        if len(fetchers) == 1:
            for target in pending:
                visit(target)
        else:
            with ThreadPoolExecutor(max_workers=len(fetchers)) as executor:
                _drain(executor.map(visit, pending))
    except BaseException:
        # 中断も含めて、残りのワーカーを空振りさせてから抜ける
        # (executor は投入済みのタスクを待つため、止めないと最後まで走ってしまう)。
        state.stop()
        raise

    if state.abort_reason is not None:
        raise DetailError(state.abort_reason)
    return run


def _drain(results: Iterable[None]) -> None:
    """map の結果を最後まで引き取る (例外があればここで上がる)。"""
    for _ in results:
        pass


class _RunState:
    """並列でも 1 つの実行として数えるための、共有された進捗と打ち切り判断。"""

    def __init__(
        self,
        *,
        cache: DetailCache,
        run: DetailRun,
        remaining: int,
        now: Callable[[], datetime],
        clock: Callable[[], float],
        progress_every: int,
        failure_limit: int,
    ) -> None:
        self._cache = cache
        self._run = run
        self._pending = remaining
        self._now = now
        self._clock = clock
        self._progress_every = progress_every
        self._failure_limit = failure_limit
        self._lock = threading.Lock()
        self._started_at = clock()
        self._consecutive_failures = 0
        self._stop = threading.Event()
        self.abort_reason: str | None = None

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()

    def handle(self, fetcher: Fetcher, target: Target) -> None:
        try:
            html = fetcher.get(target.url)
        except FetchError as error:
            if error.status >= 500 and truncated(error.body):
                # 相手の不具合で途中までしか描かれていない。欠けているのは切れた
                # 位置の後ろだけなので、捨てずに残して印を付ける (ADR 0030)。
                self._record(
                    target,
                    ok=True,
                    html=error.body,
                    error=str(error),
                    issue=ISSUE_TRUNCATED,
                    http_status=error.status,
                )
                return
            self._record(target, ok=False, html=None, error=str(error), http_status=error.status)
            return
        # 200 でもエラーページのことがある。掴んだら失敗として扱い、キャッシュに
        # 残さない (残すと取得済みと見なして二度と取り直せない。ADR 0011)。
        if reason := rejected(html):
            self._record(target, ok=False, html=None, error=reason)
        else:
            self._record(target, ok=True, html=html, error="")

    def _record(
        self,
        target: Target,
        *,
        ok: bool,
        html: bytes | None,
        error: str,
        issue: str = "",
        http_status: int = 0,
    ) -> None:
        self._cache.record(
            DetailEntry(
                category_code=target.category_code,
                kanri_taishou_id=target.kanri_taishou_id,
                ok=ok,
                byte_count=len(html) if html is not None else 0,
                fetched_at=self._now().isoformat(timespec="seconds"),
                error=error,
                issue=issue,
                http_status=http_status,
            ),
            html,
        )
        with self._lock:
            if ok:
                self._run.fetched += 1
            else:
                self._run.failed += 1
            if ok and not issue:
                self._consecutive_failures = 0
            else:
                # 途中切れも連続失敗に数える。相手がエラーを返していることに
                # 変わりはなく、広い範囲で切れ始めたら今までどおり打ち切る。
                self._consecutive_failures += 1
                if issue:
                    self._run.truncated += 1
                    logger.warning(
                        "%s (%s) は相手の不具合で途中までしか取れなかった: %s",
                        target.key,
                        target.name,
                        error,
                    )
                else:
                    logger.warning("%s (%s) の取得に失敗した: %s", target.key, target.name, error)
                if self._consecutive_failures >= self._failure_limit:
                    self.abort_reason = (
                        f"{self._consecutive_failures} 件続けて失敗した。"
                        "相手が落ちているか、こちらが弾かれている可能性がある。"
                        "取得済みは記録済みなので、間隔を空けてから同じコマンドで再開できる"
                    )
                    self.stop()
            self._log_progress()

    def _log_progress(self) -> None:
        finished = self._run.fetched + self._run.failed
        if finished % self._progress_every and finished != self._pending:
            return
        elapsed = self._clock() - self._started_at
        eta = elapsed / finished * (self._pending - finished) if finished else 0.0
        logger.info(
            "%d/%d 件 (%.1f%%) 失敗 %d 件 残り約 %s",
            finished,
            self._pending,
            finished / self._pending * 100,
            self._run.failed,
            format_duration(eta),
        )


def format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f} 秒"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分"
    return f"{seconds / 3600:.1f} 時間"


@dataclass(frozen=True)
class DetailSummary:
    """キャッシュの充足具合。台帳から作った対象と突き合わせて数える。"""

    total: int
    fetched: int
    failed: int
    truncated: int = 0
    """取れはしたが、相手の不具合で途中までしか描かれていない数 (ADR 0030)。
    ``fetched`` にも含む。"""

    @property
    def missing(self) -> int:
        """一度も取りに行っていない数。"""
        return self.total - self.fetched - self.failed

    @property
    def is_complete(self) -> bool:
        return self.total > 0 and self.fetched == self.total


def summarize_details(cache: DetailCache, targets: Sequence[Target]) -> DetailSummary:
    fetched = 0
    failed = 0
    truncated = 0
    for target in targets:
        entry = cache.entries.get(target.key)
        if entry is None:
            continue
        if entry.ok and cache.html_path(target.category_code, target.kanri_taishou_id).exists():
            fetched += 1
            truncated += bool(entry.issue)
        else:
            failed += 1
    return DetailSummary(total=len(targets), fetched=fetched, failed=failed, truncated=truncated)


def format_detail_summary(summary: DetailSummary, failures: Sequence[DetailEntry] = ()) -> str:
    """取得結果を人が読める形にする。何をすれば埋まるかまで書く。"""
    if summary.total == 0:
        return "台帳が空。先に heritage-crawler fetch-ledger を実行する"
    lines = [
        f"対象 {summary.total:,} 件 / 取得済み {summary.fetched:,} 件 "
        f"({summary.fetched / summary.total * 100:.1f}%) / "
        f"失敗 {summary.failed:,} 件 / 未取得 {summary.missing:,} 件"
    ]
    if summary.truncated:
        lines.append(
            f"うち相手の不具合で途中までしか取れなかった {summary.truncated:,} 件 "
            "(印を付けて残した。fetch-detail --retry-failed で取り直せる)"
        )
    for entry in failures[:5]:
        lines.append(
            f"  失敗: {detail_key(entry.category_code, entry.kanri_taishou_id)} {entry.error}"
        )
    if len(failures) > 5:
        lines.append(f"  ほか {len(failures) - 5:,} 件")
    if failures:
        lines.append("失敗ぶんは fetch-detail --retry-failed で拾い直せる")
    elif not summary.is_complete:
        lines.append("残りは fetch-detail で続きから取れる")
    return "\n".join(lines)
