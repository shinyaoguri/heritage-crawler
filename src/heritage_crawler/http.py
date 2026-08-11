"""公共サイトへ礼儀正しくアクセスするための HTTP クライアント。

ADR 0002 のアクセスマナー (逐次アクセス・リクエスト間隔の挿入・User-Agent への
連絡先記載) をここで守る。相手は文化庁のデータベースであり、こちらの都合で
並列化しない。並列度を上げられるようにはしてあるが (ADR 0006)、レートは
``RateLimiter`` を共有することで並列でも 1 本ぶんに収める。

依存パッケージを増やしていないのは、必要なもの (Cookie でのセッション維持と
フォーム POST) が標準ライブラリで足りるため。CSV 出力はサーバ側セッションに
依存するが、それは Cookie の保持で足り、ヘッドレスブラウザは要らない
(Issue #1 の調査所見)。
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from http.cookiejar import CookieJar
from importlib import metadata
from typing import Final, Protocol

logger = logging.getLogger(__name__)

DEFAULT_CONTACT: Final = "https://github.com/shinyaoguri/heritage-crawler"
DEFAULT_INTERVAL: Final = 1.0
DEFAULT_TIMEOUT: Final = 60.0
DEFAULT_RETRIES: Final = 3

FormFields = Sequence[tuple[str, str]]
"""フォームの送信値。

順序と重複をそのまま保つため dict ではなく組の列で扱う。CSV 出力の hidden 値は
HTML から取り出した並びのまま送るのが安全なため (ADR 0002)。
"""


class FetchError(RuntimeError):
    """取得に失敗した。再試行しても回復しなかった場合を含む。"""


class Fetcher(Protocol):
    """取得の口。テストが外部サイトへ出ないよう、ここで差し替えられるようにする。"""

    def get(self, url: str) -> bytes: ...

    def post(self, url: str, fields: FormFields) -> bytes: ...


def _package_version() -> str:
    try:
        return metadata.version("heritage-crawler")
    except metadata.PackageNotFoundError:  # pragma: no cover - 未インストール実行時
        return "0.0.0"


def user_agent(contact: str = DEFAULT_CONTACT) -> str:
    """連絡先を含む User-Agent。何者からのアクセスか先方が辿れるようにする。"""
    return f"heritage-crawler/{_package_version()} (+{contact})"


class RateLimiter:
    """リクエストの間隔を空けるゲート。

    間隔は前のリクエストの**開始**からではなく完了から測る。応答が遅いときに
    間隔が詰まらないようにするため。

    1 つを複数のクライアントで共有できる。並列に取りに行っても、相手から見た
    レートは 1 本ぶんに収まる (ADR 0002 の逐次アクセスの精神を保ったまま、
    応答待ちの時間だけを重ねられる)。
    """

    def __init__(
        self,
        interval: float = DEFAULT_INTERVAL,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval < 0:
            raise ValueError("interval に負の値は指定できない")
        self.interval = interval
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._finished_at: float | None = None

    def wait(self) -> None:
        """前のリクエストの完了から interval 秒が経つまで待つ。"""
        with self._lock:
            if self._finished_at is None:
                return
            remaining = self.interval - (self._clock() - self._finished_at)
        # 眠るのはロックの外。並列時に他のワーカーまで足止めしないため。
        if remaining > 0:
            self._sleep(remaining)

    def done(self) -> None:
        """リクエストが終わったことを記録する。成否は問わない。"""
        with self._lock:
            self._finished_at = self._clock()


class PoliteClient:
    """Cookie を保持し、リクエスト間隔を空けて逐次アクセスするクライアント。

    並列に取りに行くときは、クライアントをワーカーごとに作って ``limiter`` を
    共有する。urllib の opener をスレッド間で共有せずに済み、レートは 1 本に
    保てる。
    """

    def __init__(
        self,
        *,
        contact: str = DEFAULT_CONTACT,
        interval: float = DEFAULT_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
        limiter: RateLimiter | None = None,
        opener: urllib.request.OpenerDirector | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if retries < 1:
            raise ValueError("retries は 1 以上にする")
        self._opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        self._user_agent = user_agent(contact)
        self._limiter = limiter or RateLimiter(interval, sleep=sleep, clock=clock)
        self._timeout = timeout
        self._retries = retries
        self._sleep = sleep

    def get(self, url: str) -> bytes:
        return self._open(urllib.request.Request(url, method="GET"))

    def post(self, url: str, fields: FormFields) -> bytes:
        body = urllib.parse.urlencode(list(fields), encoding="utf-8").encode("ascii")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return self._open(request)

    def _open(self, request: urllib.request.Request) -> bytes:
        request.add_header("User-Agent", self._user_agent)
        request.add_header("Accept-Language", "ja")
        last_error: Exception | None = None
        for attempt in range(1, self._retries + 1):
            self._limiter.wait()
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    body: bytes = response.read()
                return body
            except urllib.error.HTTPError as error:
                error.close()
                # 4xx はこちらの組み立てが誤っている。繰り返しても同じなので即座に諦める。
                if error.code < 500:
                    raise FetchError(
                        f"{request.method} {request.full_url} が {error.code} を返した"
                    ) from error
                last_error = error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
            finally:
                self._limiter.done()
            if attempt < self._retries:
                backoff = self._limiter.interval * 2**attempt
                logger.warning(
                    "%s %s に失敗した (%s)。%.1f 秒待って再試行する (%d/%d)",
                    request.method,
                    request.full_url,
                    last_error,
                    backoff,
                    attempt,
                    self._retries - 1,
                )
                self._sleep(backoff)
        raise FetchError(
            f"{request.method} {request.full_url} が {self._retries} 回とも失敗した"
        ) from last_error
