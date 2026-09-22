"""コマンドラインのテスト。取得は走らせず、引数の解釈と報告だけを見る。"""

from __future__ import annotations

import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import area_named, fixture, put_detail, put_ledger
from heritage_crawler import cli
from heritage_crawler.cache import ISSUE_TRUNCATED, DetailCache, LedgerCache
from heritage_crawler.catalog import (
    SELECTED,
    TARGET_CATEGORIES,
    TARGET_DATASETS,
)
from heritage_crawler.cli import ALL_AREAS, build_parser, main
from heritage_crawler.detail import Presence
from heritage_crawler.ledger import LedgerRun
from heritage_crawler.readme import BEGIN_MARKER, DATASETS_BEGIN_MARKER, END_MARKER
from heritage_crawler.status import STATUS_FILENAME, checked_today
from heritage_crawler.update import ROTATION_SLOTS, rotation_slot

PRESERVATION_DISTRICTS = "important-preservation-districts-for-groups-of-traditional-buildings"


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
        build_parser().parse_args(["fetch-ledger", "--category", "999"])


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
    """未正規化の受け皿 (IRREGULAR_AREAS) と無形文化財の 9 地域まで数に入っているか。

    地域は選択肢を出せない (metavar で伏せている) ぶん、ヘルプの数が実態より
    小さいと、取りに行っている地域を黙って隠すことになる。
    """
    stated = re.search(r"選べるのは全 (\d+) 地域", _help_of("fetch-ledger", capsys))
    assert stated is not None, "ヘルプの書式が変わった。テストの読み取りを合わせる"
    assert int(stated.group(1)) == len(ALL_AREAS)


def test_地域を指定しなければ分類ごとの分割軸に任せる(
    cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """地域は分類ごとに違う (#74)。全分類に同じ地域一式を投げない。

    ``--area`` を渡さないと下流へ None が届き、分類ごとの分割軸が使われる。
    報告の「未取得 N 地域」がその N をそのまま映す。
    """
    assert build_parser().parse_args(["fetch-ledger"]).areas is None

    main(["--cache-dir", str(cache_dir), "report-ledger"])
    printed = capsys.readouterr().out

    assert "未取得 51 地域" in printed  # 101 など
    assert "未取得 1 地域" in printed  # 304 / 303 / 313 / 323 / 312 (全国 1 回)
    assert "未取得 60 地域" in printed  # 302 / 322 (都道府県 + 9 地域)


def test_既定のレート上限は_1_req_s() -> None:
    """これを超えると相手が 200 でエラーページを返す (ADR 0011)。"""
    assert build_parser().parse_args(["fetch-ledger"]).interval == pytest.approx(1.0)


def test_報告は取得せずに出せる(cache_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """通信が絡まないので、キャッシュが空でも動く。"""
    assert main(["--cache-dir", str(cache_dir), "report-ledger"]) == 0
    printed = capsys.readouterr().out
    for code in ("101", "102", "103"):
        assert code in printed
    # 分割軸は分類で違う (#74)。101 は 51 地域、304 は全国 1 回だけ。
    assert "未取得 51 地域" in printed
    assert "未取得 1 地域" in printed


def _fetch_ledgers_returning(
    monkeypatch: pytest.MonkeyPatch, run: LedgerRun
) -> None:
    """取得そのものは走らせず、結果だけを差し替える (外部サイトへ出ない)。"""
    monkeypatch.setattr(cli, "fetch_ledgers", lambda *args, **kwargs: run)


def test_台帳を取り切れなければ_1_を返す(
    cache_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """週次はこの終了コードで retry.sh の次の回を呼ぶ (ADR 0022)。"""
    _fetch_ledgers_returning(monkeypatch, LedgerRun(fetched=3, failures=["102 × 三重県"]))

    assert main(["--cache-dir", str(cache_dir), "fetch-ledger"]) == 1
    # 取れたぶんの報告は失敗があっても出す。どこまで進んだかが次の回の入力になる。
    assert "102" in capsys.readouterr().out


def test_全部取れたら_0_を返す(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fetch_ledgers_returning(monkeypatch, LedgerRun(fetched=3))

    assert main(["--cache-dir", str(cache_dir), "fetch-ledger"]) == 0


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


def test_失敗の拾い直しは途中までしか取れなかったぶんも取り直す(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """途中切れは取得済みの扱いなので、force を付けないと飛ばされる (ADR 0030)。"""
    put_ledger(LedgerCache(cache_dir), SELECTED, area_named("京都府"), ["16", "17"])
    detail = DetailCache(cache_dir)
    put_detail(detail, SELECTED, "16", fixture("detail_103.html"))
    put_detail(detail, SELECTED, "17", fixture("detail_103.html"), issue=ISSUE_TRUNCATED)
    called: dict[str, object] = {}

    def fake_fetch(fetchers, cache, targets, *, force):  # type: ignore[no-untyped-def]
        called.update(keys=[target.key for target in targets], force=force)

    monkeypatch.setattr(cli, "fetch_details", fake_fetch)

    assert main(["--cache-dir", str(cache_dir), "fetch-detail", "--retry-failed"]) == 0
    assert called == {"keys": ["103/17"], "force": True}


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


def test_差分更新の巡回は既定で実行週(cache_dir: Path) -> None:
    """週をまたいで走らせても、その週の 1/52 を取る (ADR 0020)。"""
    assert build_parser().parse_args(["update-records"]).slot is None


@pytest.mark.parametrize("value", ["0", "53"])
def test_ありえない巡回の枠は受け付けない(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["update-records", "--slot", value])


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
            "--slot",
            "1",
            "--dry-run",
        ]
    )

    assert status == 0
    assert {path: path.read_bytes() for path in sorted(out.rglob("*")) if path.is_file()} == before
    # 確認日も書かない — 見にいっていないのに「確かめた」と残すのが一番たちが悪い
    assert not (out / PRESERVATION_DISTRICTS / STATUS_FILENAME).exists()
    printed = capsys.readouterr().out
    assert "前回の出力 1 件 / 巡回の枠 1/52" in printed


def test_確認日は既定で日本時間の今日になる(cache_dir: Path, tmp_path: Path) -> None:
    """週次は日本時間の未明に走る。UTC で切ると前日の日付が残る (ADR 0023)。"""
    put_ledger(LedgerCache(cache_dir), SELECTED, area_named("京都府"), ["16"])
    put_detail(DetailCache(cache_dir), SELECTED, "16", fixture("detail_103.html"))
    out = tmp_path / "data"
    assert main(["--cache-dir", str(cache_dir), "build-records", "--output-dir", str(out)]) == 0

    status = main(
        ["--cache-dir", str(cache_dir), "update-records", "--output-dir", str(out), "--slot", "1"]
    )

    assert status == 0
    payload = json.loads(
        (out / PRESERVATION_DISTRICTS / STATUS_FILENAME).read_text(encoding="utf-8")
    )
    assert payload["checked_date"] == checked_today(datetime.now(UTC))
    assert payload["repo"] == PRESERVATION_DISTRICTS


def test_確認日は日付として読めないと受け取らない(capsys: pytest.CaptureFixture[str]) -> None:
    """読めない値は各データリポジトリに残り、次の週まで直せない。"""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["update-records", "--checked-date", "2026/08/24"])

    assert "日付は YYYY-MM-DD で書く" in capsys.readouterr().err


def test_前回の台帳を渡すと値の変化を見つける(
    cache_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CSV 同士の突き合わせが CLI から効くこと (ADR 0020)。

    **相手先へは出ない。** 前回の台帳も今回の台帳も手元のファイル。
    """
    ledger = LedgerCache(cache_dir)
    put_ledger(ledger, SELECTED, area_named("京都府"), ["16"], values={"名称": "産寧坂"})
    put_detail(DetailCache(cache_dir), SELECTED, "16", fixture("detail_103.html"))
    out = tmp_path / "data"
    assert main(["--cache-dir", str(cache_dir), "build-records", "--output-dir", str(out)]) == 0

    # 前回の台帳は「名称が違う」状態にしておく = 今回その 1 件が変わったことになる
    previous = tmp_path / "previous"
    shutil.copytree(ledger.ledger_dir, previous)
    path = next(previous.rglob("*.csv"))
    path.write_bytes(path.read_bytes().replace("産寧坂".encode(), "旧・産寧坂".encode()))

    status = main(
        [
            "--cache-dir",
            str(cache_dir),
            "update-records",
            "--output-dir",
            str(out),
            "--previous-ledger",
            str(previous),
            "--dry-run",
        ]
    )

    assert status == 0
    printed = capsys.readouterr().out
    assert "1 ファイルが前回と違う" in printed
    assert "台帳の値が変わった 1" in printed


def test_存在を確かめてから落とす(
    cache_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """落とすかどうかを詳細ページの返答で決め直す配線 (ADR 0021)。

    **相手先へは出ない** — 存在確認だけを身代わりに差し替える。巡回に当たらない枠を
    選んであるので、取得そのものは 1 件も走らない。

    ここでは全国件数を記録していないので 103 の網羅性は確かめられない状態にある。
    それでも「無い」と答えたものは落とす — 102 と同じ、集計では確かめられない
    分類での振る舞いをそのまま試している。
    """
    ledger = LedgerCache(cache_dir)
    put_ledger(ledger, SELECTED, area_named("京都府"), ["16", "17"])
    for managed_id in ("16", "17"):
        put_detail(DetailCache(cache_dir), SELECTED, managed_id, fixture("detail_103.html"))
    out = tmp_path / "data"
    assert main(["--cache-dir", str(cache_dir), "build-records", "--output-dir", str(out)]) == 0

    put_ledger(ledger, SELECTED, area_named("京都府"), ["16"])  # 17 が台帳から消えた
    monkeypatch.setattr(cli, "probe_presence", lambda *_, **__: {"103/17": Presence.GONE})
    slot = (rotation_slot("103/16") + 2) % ROTATION_SLOTS + 1

    status = main(
        [
            "--cache-dir",
            str(cache_dir),
            "update-records",
            "--output-dir",
            str(out),
            "--slot",
            str(slot),
        ]
    )

    assert status == 0
    removed = [
        json.loads(line)
        for line in (out / PRESERVATION_DISTRICTS / "removed.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [entry["managed_id"] for entry in removed] == ["17"]
    assert removed[0]["conclusion"] == "delisted"
    assert removed[0]["evidence"]["detail_page"] == "gone"
    assert removed[0]["evidence"]["category_complete"] is False


def test_前回の台帳が無ければ値の変化は見つけられない(
    cache_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """**それでも追加・削除・巡回は働く。** 止めるほどのことではない。"""
    put_ledger(LedgerCache(cache_dir), SELECTED, area_named("京都府"), ["16"])
    put_detail(DetailCache(cache_dir), SELECTED, "16", fixture("detail_103.html"))
    out = tmp_path / "data"
    assert main(["--cache-dir", str(cache_dir), "build-records", "--output-dir", str(out)]) == 0

    status = main(
        ["--cache-dir", str(cache_dir), "update-records", "--output-dir", str(out), "--dry-run"]
    )

    assert status == 0
    assert "前回の台帳が渡されていない" in caplog.text


def render_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """`render-readme` を試すための、データ 1 件と差し込み口だけの README。

    差し込み口は 2 つ置く — 件数表と出力先リポジトリの表。**閉じは共通**なので、
    取り違えずに書き換えられることもここで見ている。
    """
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
    readme.write_text(
        f"前書き\n\n{BEGIN_MARKER}\n{END_MARKER}\n\n"
        f"なか書き\n\n{DATASETS_BEGIN_MARKER}\n{END_MARKER}\n\n後書き\n",
        encoding="utf-8",
    )
    return out, readme


def test_件数表を書き直す(tmp_path: Path) -> None:
    out, readme = render_fixture(tmp_path)

    status = main(["render-readme", "--output-dir", str(out), "--readme", str(readme)])

    assert status == 0
    text = readme.read_text(encoding="utf-8")
    assert "| 103 | 重要伝統的建造物群保存地区 | 選定 | 126 | 1 | 1 |" in text
    assert "| `world-heritage-sites` | 901 世界遺産 |" in text
    assert text.startswith("前書き\n\n")
    assert "\n\nなか書き\n\n" in text
    assert text.endswith("\n\n後書き\n")


def test_ずれていれば_check_は異常終了する(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """月次はここで気付く。あるべき表を出して、そのまま Issue に載せられるようにする。"""
    out, readme = render_fixture(tmp_path)

    status = main(["render-readme", "--output-dir", str(out), "--readme", str(readme), "--check"])

    assert status == 1
    printed = capsys.readouterr().out
    assert "| **計** |" in printed
    assert "| `world-heritage-sites` | 901 世界遺産 |" in printed
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
