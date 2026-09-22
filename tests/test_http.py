"""HTTP クライアントのテスト。

opener を差し替えているので通信は発生しない。ここで守りたいのはアクセスマナー
(レートの上限・連絡先・gzip) と、失敗時のふるまい。レートの決まり方は ADR 0010。
"""

from __future__ import annotations

import email.message
import gzip
import io
import urllib.error
import urllib.request
from typing import Any

import pytest

from heritage_crawler.http import FetchError, PoliteClient, RateLimiter


class FakeResponse(io.BytesIO):
    """本文とヘッダを持つ応答。実物と同じく Content-Encoding を見せる。"""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.headers = email.message.Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value


class FakeOpener:
    """用意した応答を順に返す opener。例外を混ぜると失敗を再現できる。"""

    def __init__(self, *responses: bytes | FakeResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float | None] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, FakeResponse) else FakeResponse(response)


class FakeTime:
    """眠った時間だけ進む時計。実時間を待たずに間隔の検査ができる。"""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def http_error(
    code: int, body: bytes = b"", headers: dict[str, str] | None = None
) -> urllib.error.HTTPError:
    message = email.message.Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return urllib.error.HTTPError("https://example.test/", code, "boom", message, io.BytesIO(body))


def make_client(opener: FakeOpener, clock: FakeTime, **kwargs: Any) -> PoliteClient:
    return PoliteClient(opener=opener, sleep=clock.sleep, clock=clock, **kwargs)


def test_User_Agent_に連絡先を載せる() -> None:
    opener = FakeOpener(b"ok")
    make_client(opener, FakeTime(), contact="https://example.test/contact").get(
        "https://example.test/"
    )
    agent = opener.requests[0].get_header("User-agent")
    assert agent.startswith("heritage-crawler/")
    assert "(+https://example.test/contact)" in agent


def test_POST_は順序と重複を保ったまま_UTF_8_で送る() -> None:
    opener = FakeOpener(b"ok")
    make_client(opener, FakeTime()).post(
        "https://example.test/", (("b", "1"), ("a", "北海道"), ("b", "2"))
    )
    request = opener.requests[0]
    assert request.data == b"b=1&a=%E5%8C%97%E6%B5%B7%E9%81%93&b=2"
    assert request.get_header("Content-type") == "application/x-www-form-urlencoded"


def test_2_回目以降のリクエストは間隔を空ける() -> None:
    """相手は公共サイト。レートの上限は間隔が決める (ADR 0010)。"""
    opener = FakeOpener(b"one", b"two")
    clock = FakeTime()
    client = make_client(opener, clock, interval=1.5)

    client.get("https://example.test/1")
    assert clock.slept == []  # 1 回目は待たない

    client.get("https://example.test/2")
    assert clock.slept == [1.5]


def test_間隔は複数のクライアントで共有できる() -> None:
    """並列に取りに行っても、レートの上限は間隔が決めた 1 本ぶん (ADR 0010)。"""
    clock = FakeTime()
    limiter = RateLimiter(1.5, sleep=clock.sleep, clock=clock)
    first = PoliteClient(opener=FakeOpener(b"one"), limiter=limiter)
    second = PoliteClient(opener=FakeOpener(b"two"), limiter=limiter)

    first.get("https://example.test/1")
    assert clock.slept == []

    second.get("https://example.test/2")
    assert clock.slept == [1.5]  # 別のクライアントでも待つ


def test_間隔は応答時間に上乗せしない() -> None:
    """完了から測ると応答のぶんレートが落ちる。開始から測る (ADR 0010)。"""
    clock = FakeTime()
    limiter = RateLimiter(1.0, sleep=clock.sleep, clock=clock)

    limiter.wait()
    clock.now += 0.6  # 応答に 0.6 秒かかった
    limiter.wait()

    # 発射は 0.0 秒と 1.0 秒。応答ぶんを足した 1.6 秒にはならない
    assert clock.now == pytest.approx(1.0)
    assert clock.slept == [pytest.approx(0.4)]


def test_共有した_limiter_は同じ時刻に発射させない() -> None:
    """全ワーカーが同じ残り時間を計算して同時に起きると逐次にならない (ADR 0010)。"""
    clock = FakeTime()
    limiter = RateLimiter(1.0, sleep=clock.sleep, clock=clock)

    fired = []
    for _ in range(3):  # ワーカー 3 本ぶんの予約を続けて取る
        limiter.wait()
        fired.append(clock.now)

    assert fired == [pytest.approx(0.0), pytest.approx(1.0), pytest.approx(2.0)]


def test_gzip_で返ってきたら展開する() -> None:
    """返ってきたら展開する。現時点でサーバは gzip を返さないが、対応すれば効く
    (ADR 0010)。"""
    body = gzip.compress(b"<html>ok</html>")
    opener = FakeOpener(FakeResponse(body, {"Content-Encoding": "gzip"}))

    fetched = make_client(opener, FakeTime()).get("https://example.test/")

    assert fetched == b"<html>ok</html>"
    assert opener.requests[0].get_header("Accept-encoding") == "gzip"


def test_5xx_は間を空けて再試行する() -> None:
    opener = FakeOpener(http_error(503), b"ok")
    clock = FakeTime()
    assert make_client(opener, clock, interval=1.0).get("https://example.test/") == b"ok"
    assert len(opener.requests) == 2
    assert clock.slept[0] == pytest.approx(2.0)  # 待ち時間を伸ばしてから再試行する


def test_再試行の待ち時間は間隔を詰めても縮まない() -> None:
    """5xx を返している相手への再試行まで速くしてはいけない (ADR 0010)。"""
    opener = FakeOpener(http_error(503), b"ok")
    clock = FakeTime()

    make_client(opener, clock, interval=0.05).get("https://example.test/")

    assert clock.slept[0] == pytest.approx(2.0)


def test_4xx_は再試行しない() -> None:
    """組み立てが誤っている side なので、繰り返しても相手に迷惑なだけ。"""
    opener = FakeOpener(http_error(404), b"ok")
    with pytest.raises(FetchError, match="404"):
        make_client(opener, FakeTime()).get("https://example.test/")
    assert len(opener.requests) == 1


def test_再試行しきっても駄目なら_FetchError() -> None:
    opener = FakeOpener(*[urllib.error.URLError("timed out")] * 3)
    with pytest.raises(FetchError, match="3 回とも失敗"):
        make_client(opener, FakeTime(), retries=3).get("https://example.test/")
    assert len(opener.requests) == 3


def test_エラー応答の本文とステータスを_FetchError_に持たせる() -> None:
    """5xx の本文が途中まで描かれた詳細ページのことがある (#107 / ADR 0030)。

    使うかどうかは呼び手が決める。持たせるのは**最後の**応答。
    """
    opener = FakeOpener(
        http_error(500, b"first"),
        http_error(500, gzip.compress(b"last"), {"Content-Encoding": "gzip"}),
    )
    with pytest.raises(FetchError) as caught:
        make_client(opener, FakeTime(), retries=2).get("https://example.test/")
    assert (caught.value.status, caught.value.body) == (500, b"last")


def test_最後の試行が無応答なら前の本文を持ち越さない() -> None:
    opener = FakeOpener(http_error(500, b"partial"), urllib.error.URLError("timed out"))
    with pytest.raises(FetchError) as caught:
        make_client(opener, FakeTime(), retries=2).get("https://example.test/")
    assert (caught.value.status, caught.value.body) == (0, b"")


def test_4xx_の本文も持たせる() -> None:
    opener = FakeOpener(http_error(404, b"not here"))
    with pytest.raises(FetchError) as caught:
        make_client(opener, FakeTime()).get("https://example.test/")
    assert (caught.value.status, caught.value.body) == (404, b"not here")


def test_タイムアウトを_opener_へ渡す() -> None:
    opener = FakeOpener(b"ok")
    make_client(opener, FakeTime(), timeout=12.5).get("https://example.test/")
    assert opener.timeouts == [12.5]


@pytest.mark.parametrize("kwargs", [{"interval": -1.0}, {"retries": 0}])
def test_ありえない設定は受け付けない(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        PoliteClient(opener=FakeOpener(), **kwargs)
