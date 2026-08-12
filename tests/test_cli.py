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


def test_既定のレート上限は_1_req_s() -> None:
    """これを超えると相手が 200 でエラーページを返す (ADR 0011)。"""
    assert build_parser().parse_args(["fetch-ledger"]).interval == pytest.approx(1.0)


def test_報告は取得せずに出せる(cache_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """通信が絡まないので、キャッシュが空でも動く。"""
    assert main(["--cache-dir", str(cache_dir), "report-ledger"]) == 0
    printed = capsys.readouterr().out
    for code in ("101", "102", "103"):
        assert code in printed
    assert "未取得 51 地域" in printed


def test_一覧の突き合わせは既定で回収しない() -> None:
    """検査と修復は分ける。書き換えるときは --recover を明示する。"""
    assert build_parser().parse_args(["audit-listing"]).recover is False


def test_一覧の突き合わせに取り直しの指定は無い() -> None:
    """毎回すべて取り直すコマンドなので、--force は意味を持たない。"""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["audit-listing", "--force"])


def test_棟に展開される分類だけを選んだら突き合わせない(cache_dir: Path) -> None:
    """102 は一覧 (指定単位) と CSV (棟単位) を突き合わせられない。"""
    assert main(["--cache-dir", str(cache_dir), "audit-listing", "--category", "102"]) == 1


def test_負の間隔は受け付けない() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["fetch-detail", "--interval", "-1"])


def test_詳細取得の既定は逐次() -> None:
    """間隔 1 秒なら 1 本で上限を出しきる。増やす理由が無い (ADR 0011)。"""
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
