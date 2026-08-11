"""HTTP クライアントのテスト。

opener を差し替えているので通信は発生しない。ここで守りたいのは
ADR 0002 のアクセスマナー (間隔・連絡先) と、失敗時のふるまい。
"""

from __future__ import annotations

import email.message
import io
import urllib.error
import urllib.request
from typing import Any

import pytest

from heritage_crawler.http import FetchError, PoliteClient


class FakeOpener:
    """用意した応答を順に返す opener。例外を混ぜると失敗を再現できる。"""

    def __init__(self, *responses: bytes | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float | None] = []

    def open(self, request: urllib.request.Request, timeout: float | None = None) -> Any:
        self.requests.append(request)
        self.timeouts.append(timeout)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(response)


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


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.test/", code, "boom", email.message.Message(), io.BytesIO(b"")
    )


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
    """相手は公共サイト。並列化も詰め込みもしない (ADR 0002)。"""
    opener = FakeOpener(b"one", b"two")
    clock = FakeTime()
    client = make_client(opener, clock, interval=1.5)

    client.get("https://example.test/1")
    assert clock.slept == []  # 1 回目は待たない

    client.get("https://example.test/2")
    assert clock.slept == [1.5]


def test_5xx_は間を空けて再試行する() -> None:
    opener = FakeOpener(http_error(503), b"ok")
    clock = FakeTime()
    assert make_client(opener, clock, interval=1.0).get("https://example.test/") == b"ok"
    assert len(opener.requests) == 2
    assert clock.slept[0] == pytest.approx(2.0)  # 待ち時間を伸ばしてから再試行する


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


def test_タイムアウトを_opener_へ渡す() -> None:
    opener = FakeOpener(b"ok")
    make_client(opener, FakeTime(), timeout=12.5).get("https://example.test/")
    assert opener.timeouts == [12.5]


@pytest.mark.parametrize("kwargs", [{"interval": -1.0}, {"retries": 0}])
def test_ありえない設定は受け付けない(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        PoliteClient(opener=FakeOpener(), **kwargs)
