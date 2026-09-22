"""公共サイトへ礼儀正しくアクセスするための HTTP クライアント。

アクセスマナー (レートの上限・User-Agent への連絡先記載・gzip での転送量削減) を
ここで守る。相手は文化庁のデータベースなので、レートの上限は間隔だけが決める
(ADR 0010)。``RateLimiter`` を共有すれば、並列度を上げても上限は超えない —
埋まるのは応答待ちの隙間だけで、同時に飛ぶ本数が同時接続の上限になる。

依存パッケージを増やしていないのは、必要なもの (Cookie でのセッション維持と
フォーム POST) が標準ライブラリで足りるため。CSV 出力はサーバ側セッションに
依存するが、それは Cookie の保持で足り、ヘッドレスブラウザは要らない
(Issue #1 の調査所見)。
"""

from __future__ import annotations

import gzip
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Sequence
from http.cookiejar import CookieJar
from importlib import metadata
from typing import Final, Protocol

logger = logging.getLogger(__name__)

DEFAULT_CONTACT: Final = "https://github.com/shinyaoguri/heritage-crawler"
DEFAULT_INTERVAL: Final = 1.0
"""リクエストの間隔 = レート上限 1 req/s。

**これ以上詰めない。** 4 req/s で回したら相手が応答の 15% を 200 のエラーページに
した (ADR 0011)。間隔は前のリクエストの開始から測るので、この値がそのまま上限になる。
"""

DEFAULT_TIMEOUT: Final = 60.0
DEFAULT_RETRIES: Final = 3
RETRY_BACKOFF: Final = 1.0
"""再試行の待ち時間の基数 (2 秒・4 秒と伸ばす)。

**間隔から導かない。** 間隔を詰めたときに、5xx を返している相手への再試行まで
速くなってしまうため (ADR 0010)。
"""

FormFields = Sequence[tuple[str, str]]
"""フォームの送信値。

順序と重複をそのまま保つため dict ではなく組の列で扱う。CSV 出力の hidden 値は
HTML から取り出した並びのまま送るのが安全なため (ADR 0002)。
"""


class FetchError(RuntimeError):
    """取得に失敗した。再試行しても回復しなかった場合を含む。

    相手が HTTP のエラーで答えたときは、最後の応答のステータスと本文を持つ。
    **5xx の本文が途中まで描かれた詳細ページのことがある** (#107 / ADR 0030)。
    使えるかどうかを決めるのは呼び手で、ここは捨てずに渡すだけ。
    """

    def __init__(self, message: str, *, status: int = 0, body: bytes = b"") -> None:
        super().__init__(message)
        self.status = status
        """最後の応答の HTTP ステータス。応答が無かった (タイムアウトなど) なら 0。"""

        self.body = body
        """最後の応答の本文 (gzip は展開済み)。"""


class Fetcher(Protocol):
    """取得の口。テストが外部サイトへ出ないよう、ここで差し替えられるようにする。"""

    def get(self, url: str) -> bytes: ...

    def post(self, url: str, fields: FormFields) -> bytes: ...


def _decoded(response: object) -> bytes:
    """応答の本文を取り出す。gzip で返ってきたら展開する (ADR 0010)。

    ``Content-Encoding`` を持たない応答 (テストの身代わりなど) はそのまま返す。
    """
    body: bytes = response.read()  # type: ignore[attr-defined]
    headers = getattr(response, "headers", None)
    if headers is not None and headers.get("Content-Encoding", "").lower() == "gzip":
        return gzip.decompress(body)
    return body


def _package_version() -> str:
    try:
        return metadata.version("heritage-crawler")
    except metadata.PackageNotFoundError:  # pragma: no cover - 未インストール実行時
        return "0.0.0"


def user_agent(contact: str = DEFAULT_CONTACT) -> str:
    """連絡先を含む User-Agent。何者からのアクセスか先方が辿れるようにする。"""
    return f"heritage-crawler/{_package_version()} (+{contact})"


class RateLimiter:
    """リクエストの発射時刻を配るゲート (ADR 0010)。

    間隔は前のリクエストの**開始**から測る。完了から測ると応答時間がそのまま
    上乗せされ、相手から見たレートが意図より遅い側にずれる (実測で 1 req/s の
    つもりが 0.61 req/s になっていた)。

    1 つを複数のクライアントで共有できる。**ワーカーごとに別の発射時刻を予約する**
    ので、同じ瞬間に 2 本以上が飛ぶことはない。レートの上限は ``1/interval`` で、
    並列度は「相手が遅くなったときにレートが自ら落ちる」上限として働く
    (並列度 C・応答 R なら実効レートは ``min(1/interval, C/R)``)。
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
        self._next_at: float | None = None

    def wait(self) -> None:
        """自分の発射時刻を予約し、その時刻まで待つ。"""
        with self._lock:
            now = self._clock()
            start_at = now if self._next_at is None else max(now, self._next_at)
            self._next_at = start_at + self.interval
        # 眠るのはロックの外。並列時に他のワーカーまで足止めしないため。
        if (remaining := start_at - now) > 0:
            self._sleep(remaining)


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
        # 1 ページ 35 KB が約 9 KB になる。相手の転送量が減る (ADR 0010)。
        request.add_header("Accept-Encoding", "gzip")
        last_error: Exception | None = None
        status, body = 0, b""
        for attempt in range(1, self._retries + 1):
            self._limiter.wait()
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    return _decoded(response)
            except urllib.error.HTTPError as error:
                status, body = error.code, _error_body(error)
                # 4xx はこちらの組み立てが誤っている。繰り返しても同じなので即座に諦める。
                if error.code < 500:
                    raise FetchError(
                        f"{request.method} {request.full_url} が {error.code} を返した",
                        status=status,
                        body=body,
                    ) from error
                last_error = error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                # 応答が無かった試行。前の試行の本文を最後の応答として持ち越さない。
                status, body = 0, b""
                last_error = error
            if attempt < self._retries:
                backoff = RETRY_BACKOFF * 2**attempt
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
            f"{request.method} {request.full_url} が {self._retries} 回とも失敗した",
            status=status,
            body=body,
        ) from last_error


def _error_body(error: urllib.error.HTTPError) -> bytes:
    """エラー応答の本文を読んで閉じる。読めなければ空。"""
    try:
        return _decoded(error)
    except (OSError, EOFError, zlib.error):
        return b""
    finally:
        error.close()
