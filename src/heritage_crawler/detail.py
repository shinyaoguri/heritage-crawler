"""詳細取得層 — 台帳の各行から詳細ページを巡回して生 HTML を貯める
(ADR 0002 の 2 段目)。

パースはしない。取得と解析を分けておかないと、パース仕様を変えるたびに
2 万ページを取り直すことになる (ADR 0006)。

台帳と違い、詳細ページはセッションも CSRF も要らない静的な GET なので、
取得の単位は ``(台帳ID, 管理対象ID)`` だけで完結する。
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
from typing import Final

from heritage_crawler.cache import DetailCache, DetailEntry, LedgerCache, detail_key
from heritage_crawler.catalog import SEARCH_AREAS, Area, Category, detail_url
from heritage_crawler.http import Fetcher, FetchError
from heritage_crawler.ledger import read_ledger_rows

logger = logging.getLogger(__name__)

PROGRESS_EVERY: Final = 100
CONSECUTIVE_FAILURE_LIMIT: Final = 10
"""これだけ続けて失敗したら打ち切る。

相手が落ちているか、こちらが弾かれている。気付かずに 2 万回叩き続けない。
"""


class DetailError(RuntimeError):
    """詳細ページの巡回を続けられない。"""


@dataclass(frozen=True)
class Target:
    """詳細ページ 1 件の取得対象。

    ID は必ず文字列のまま扱う。管理対象ID には短い連番 (``23``) と 8 桁ゼロ詰め
    (``00003904``) が混在し、数値に変換するとゼロ詰めが落ちて到達できなくなる。
    """

    daichou_id: str
    kanri_taishou_id: str
    name: str
    """ログに出すためだけの表示名。取得には使わない。"""

    @property
    def key(self) -> str:
        return detail_key(self.daichou_id, self.kanri_taishou_id)

    @property
    def url(self) -> str:
        return detail_url(self.daichou_id, self.kanri_taishou_id)


def read_targets(
    cache: LedgerCache,
    categories: Sequence[Category],
    areas: Sequence[Area] = SEARCH_AREAS,
) -> list[Target]:
    """キャッシュ済みの台帳 CSV から取得対象を作る。

    ``(台帳ID, 管理対象ID)`` で重複を落とす。複数の都道府県に現れる指定がある
    ため (102 の琵琶湖疏水施設が滋賀県と京都府の両方に出る)。棟レベルでキーが
    完全に重なるので、ここで落とせば詳細ページを二度取りに行かずに済む。

    台帳ID は分類コードと同じ値だが、組み立てには CSV の値をそのまま使う。
    """
    targets: dict[str, Target] = {}
    for row in read_ledger_rows(cache, categories, areas):
        target = Target(
            daichou_id=row.get("台帳ID"),
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


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
        if force or not cache.is_done(target.daichou_id, target.kanri_taishou_id)
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
            self._record(target, ok=False, html=None, error=str(error))
        else:
            self._record(target, ok=True, html=html, error="")

    def _record(self, target: Target, *, ok: bool, html: bytes | None, error: str) -> None:
        self._cache.record(
            DetailEntry(
                daichou_id=target.daichou_id,
                kanri_taishou_id=target.kanri_taishou_id,
                ok=ok,
                byte_count=len(html) if html is not None else 0,
                fetched_at=self._now().isoformat(timespec="seconds"),
                error=error,
            ),
            html,
        )
        with self._lock:
            if ok:
                self._run.fetched += 1
                self._consecutive_failures = 0
            else:
                self._run.failed += 1
                self._consecutive_failures += 1
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
    for target in targets:
        entry = cache.entries.get(target.key)
        if entry is None:
            continue
        if entry.ok and cache.html_path(target.daichou_id, target.kanri_taishou_id).exists():
            fetched += 1
        else:
            failed += 1
    return DetailSummary(total=len(targets), fetched=fetched, failed=failed)


def format_detail_summary(summary: DetailSummary, failures: Sequence[DetailEntry] = ()) -> str:
    """取得結果を人が読める形にする。何をすれば埋まるかまで書く。"""
    if summary.total == 0:
        return "台帳が空。先に heritage-crawler fetch-ledger を実行する"
    lines = [
        f"対象 {summary.total:,} 件 / 取得済み {summary.fetched:,} 件 "
        f"({summary.fetched / summary.total * 100:.1f}%) / "
        f"失敗 {summary.failed:,} 件 / 未取得 {summary.missing:,} 件"
    ]
    for entry in failures[:5]:
        lines.append(
            f"  失敗: {detail_key(entry.daichou_id, entry.kanri_taishou_id)} {entry.error}"
        )
    if len(failures) > 5:
        lines.append(f"  ほか {len(failures) - 5:,} 件")
    if failures:
        lines.append("失敗ぶんは fetch-detail --retry-failed で拾い直せる")
    elif not summary.is_complete:
        lines.append("残りは fetch-detail で続きから取れる")
    return "\n".join(lines)
