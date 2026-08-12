"""コマンドラインのテスト。取得は走らせず、引数の解釈と報告だけを見る。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import area_named, fixture, put_detail, put_ledger
from heritage_crawler.cache import DetailCache, LedgerCache
from heritage_crawler.catalog import (
    SEARCH_AREAS,
    SELECTED,
    TARGET_CATEGORIES,
    TARGET_DATASETS,
)
from heritage_crawler.cli import build_parser, main
from heritage_crawler.readme import BEGIN_MARKER, END_MARKER


def _help_of(command: str, capsys: pytest.CaptureFixture[str]) -> str:
    """サブコマンドの ``--help`` を 1 行に均して返す。

    argparse は端末幅で折り返す。行の切れ目に振り回されないよう空白を畳んでから
    比べる (幅の狭い端末で落ちるテストは、直す気を失わせるだけで何も守らない)。
    """
    with pytest.raises(SystemExit):
        build_parser().parse_args([command, "--help"])
    return " ".join(capsys.readouterr().out.split())


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


def test_分類のヘルプが語彙と食い違わない(capsys: pytest.CaptureFixture[str]) -> None:
    """ヘルプの既定に分類コードを直書きすると、分類が増えた月に嘘になる。

    実際 401 を加えたとき (#27) に「既定: 101 102 103」が取り残された (#38)。
    見るのは選択肢の列挙ではなく**既定の記述**。選択肢は argparse が語彙から
    出すので初めからずれない。
    """
    stated = re.search(r"分類コード \(既定: ([^)]+)\)", _help_of("fetch-ledger", capsys))
    assert stated is not None, "ヘルプの書式が変わった。テストの読み取りを合わせる"
    assert stated.group(1).split() == [category.code for category in TARGET_CATEGORIES]


def test_地域のヘルプが語彙と食い違わない(capsys: pytest.CaptureFixture[str]) -> None:
    """未正規化の受け皿 (IRREGULAR_AREAS) まで数に入っているか。

    地域は 51 個あって選択肢を出せない (metavar で伏せている) ぶん、ヘルプの
    数が実態より小さいと、取りに行っている地域を黙って隠すことになる。
    """
    stated = re.search(r"地域名 \(既定: 全 (\d+) 地域", _help_of("fetch-ledger", capsys))
    assert stated is not None, "ヘルプの書式が変わった。テストの読み取りを合わせる"
    assert int(stated.group(1)) == len(SEARCH_AREAS)


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


def test_差分更新の巡回は既定で実行月(cache_dir: Path) -> None:
    """月をまたいで走らせても、その月の 1/12 を取る (ADR 0018)。"""
    assert build_parser().parse_args(["update-records"]).month is None


@pytest.mark.parametrize("value", ["0", "13"])
def test_ありえない月は受け付けない(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["update-records", "--month", value])


def test_台帳が無いまま差分更新を始めない(cache_dir: Path, tmp_path: Path) -> None:
    """突き合わせる相手が無い状態で通信を始めない。"""
    status = main(
        ["--cache-dir", str(cache_dir), "update-records", "--output-dir", str(tmp_path / "data")]
    )

    assert status == 1


def test_前回の出力が無ければ差分更新はしない(
    cache_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """差分の基準が無い。全件は build-records の仕事 (ADR 0018 の分担)。"""
    put_ledger(LedgerCache(cache_dir), SELECTED, area_named("京都府"), ["16"])

    status = main(
        ["--cache-dir", str(cache_dir), "update-records", "--output-dir", str(tmp_path / "data")]
    )

    assert status == 1
    assert "build-records" in caplog.text


def test_差分更新のドライランは何も書き換えない(
    cache_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """計画を見るだけ。**相手先へも出ない** (取得はここでは走らない)。"""
    put_ledger(LedgerCache(cache_dir), SELECTED, area_named("京都府"), ["16"])
    put_detail(DetailCache(cache_dir), SELECTED, "16", fixture("detail_103.html"))
    out = tmp_path / "data"
    assert main(["--cache-dir", str(cache_dir), "build-records", "--output-dir", str(out)]) == 0
    before = {path: path.read_bytes() for path in sorted(out.rglob("*")) if path.is_file()}

    status = main(
        [
            "--cache-dir",
            str(cache_dir),
            "update-records",
            "--output-dir",
            str(out),
            "--month",
            "1",
            "--dry-run",
        ]
    )

    assert status == 0
    assert {path: path.read_bytes() for path in sorted(out.rglob("*")) if path.is_file()} == before
    printed = capsys.readouterr().out
    assert "前回の出力 1 件 / 巡回の枠 1/12" in printed


def render_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """`render-readme` を試すための、データ 1 件と差し込み口だけの README。"""
    out = tmp_path / "data"
    for dataset in TARGET_DATASETS:
        keys = [{"ledger_id": "103", "managed_id": "16"}] if dataset.category is SELECTED else []
        (out / dataset.repo / "data").mkdir(parents=True)
        (out / dataset.repo / "data" / "26_kyoto.jsonl").write_text(
            "".join(json.dumps(key) + "\n" for key in keys), encoding="utf-8"
        )
        (out / dataset.repo / "meta.json").write_text(
            json.dumps({"counts": {"records": len(keys)}}), encoding="utf-8"
        )
    readme = tmp_path / "README.md"
    readme.write_text(f"前書き\n\n{BEGIN_MARKER}\n{END_MARKER}\n\n後書き\n", encoding="utf-8")
    return out, readme


def test_件数表を書き直す(tmp_path: Path) -> None:
    out, readme = render_fixture(tmp_path)

    status = main(["render-readme", "--output-dir", str(out), "--readme", str(readme)])

    assert status == 0
    text = readme.read_text(encoding="utf-8")
    assert "| 103 | 重要伝統的建造物群保存地区 | 選定 | 126 | 1 | 1 |" in text
    assert text.startswith("前書き\n\n")
    assert text.endswith("\n\n後書き\n")


def test_ずれていれば_check_は異常終了する(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """月次はここで気付く。あるべき表を出して、そのまま Issue に載せられるようにする。"""
    out, readme = render_fixture(tmp_path)

    status = main(["render-readme", "--output-dir", str(out), "--readme", str(readme), "--check"])

    assert status == 1
    assert "| **計** |" in capsys.readouterr().out
    assert BEGIN_MARKER in readme.read_text(encoding="utf-8")
    assert "103" not in readme.read_text(encoding="utf-8")


def test_一致していれば_check_は通る(tmp_path: Path) -> None:
    out, readme = render_fixture(tmp_path)
    assert main(["render-readme", "--output-dir", str(out), "--readme", str(readme)]) == 0

    status = main(["render-readme", "--output-dir", str(out), "--readme", str(readme), "--check"])

    assert status == 0


def test_データが半端なら書き換えない(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """meta.json の申告と行数が食い違う状態から README を作らない。"""
    out, readme = render_fixture(tmp_path)
    before = readme.read_text(encoding="utf-8")
    (out / "historic-sites" / "meta.json").write_text(
        json.dumps({"counts": {"records": 3}}), encoding="utf-8"
    )

    status = main(["render-readme", "--output-dir", str(out), "--readme", str(readme)])

    assert status == 1
    assert readme.read_text(encoding="utf-8") == before
    assert "食い違う" in caplog.text
