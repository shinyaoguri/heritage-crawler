"""コマンドラインのテスト。取得は走らせず、引数の解釈と報告だけを見る。"""

from __future__ import annotations

from pathlib import Path

import pytest

from heritage_crawler.cli import build_parser, main


def test_サブコマンドの指定は必須() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_知らない分類コードは受け付けない() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fetch-ledger", "--category", "901"])


def test_知らない地域名は受け付けない() -> None:
    """表記が 1 文字ずれると 0 件になり、静かに取りこぼす。"""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fetch-ledger", "--area", "北海道県"])


def test_間隔の既定は逐次アクセス() -> None:
    """相手は公共サイト (ADR 0002)。既定で詰め込まない。"""
    assert build_parser().parse_args(["fetch-ledger"]).interval >= 1.0


def test_報告は取得せずに出せる(cache_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """通信が絡まないので、キャッシュが空でも動く。"""
    assert main(["--cache-dir", str(cache_dir), "report-ledger"]) == 0
    printed = capsys.readouterr().out
    for code in ("101", "102", "103"):
        assert code in printed
    assert "未取得 51 地域" in printed


def test_負の間隔は受け付けない() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fetch-detail", "--interval", "-1"])


def test_詳細取得の既定は逐次() -> None:
    """並列は明示して初めて使う (ADR 0002)。"""
    assert build_parser().parse_args(["fetch-detail"]).concurrency == 1


@pytest.mark.parametrize("value", ["0", "99"])
def test_ありえない並列度は受け付けない(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fetch-detail", "--concurrency", value])


def test_詳細の報告は台帳が無くても動く(
    cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--cache-dir", str(cache_dir), "report-detail"]) == 0
    assert "fetch-ledger" in capsys.readouterr().out


def test_台帳が無いまま詳細を取りに行かない(cache_dir: Path) -> None:
    """対象が無いのに通信を始めない。何をすべきかは報告側に書いてある。"""
    assert main(["--cache-dir", str(cache_dir), "fetch-detail"]) == 1


def test_台帳が無ければ組み立ては失敗させる(cache_dir: Path, tmp_path: Path) -> None:
    """空のデータリポジトリを黙って作らない。"""
    status = main(
        [
            "--cache-dir",
            str(cache_dir),
            "build-records",
            "--output-dir",
            str(tmp_path / "data"),
        ]
    )

    assert status == 1
    assert not (tmp_path / "data").exists()
