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
    assert "未取得 49 地域" in printed
