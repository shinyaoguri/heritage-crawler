"""出力層のテスト。

**外部サイトへは出ない。** 台帳 CSV と生 HTML のキャッシュを ``tmp_path`` に
組み立て、そこから JSON Lines を書かせる。ここで守りたいのは ADR 0004 の
「差分が行単位で読める」— 置き場と並び順、そして 1 件の失敗で全体を止めないこと。
置き場そのもの (どのデータリポジトリへ書くか) は ADR 0009。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import FETCHED_AT, area_named, fixture, lines, put_detail, put_ledger
from heritage_crawler.cache import DetailCache, LedgerCache
from heritage_crawler.catalog import DESIGNATED, MONUMENTS, REGISTERED, SELECTED, datasets_for
from heritage_crawler.export import Reuse, build_dataset, format_report
from heritage_crawler.update import read_existing

SAMPLES = {
    REGISTERED: ("00004339", "detail_101.html", "東京都"),
    DESIGNATED: ("2594", "detail_102.html", "奈良県"),
    SELECTED: ("16", "detail_103.html", "京都府"),
}

# フィクスチャの 102 は国宝 (石上神宮拝殿)。区分を差し替えて重要文化財側も試す。
TREASURE_CLASS_CELL = "\t\t\t\t国宝\t\t\t</td>"

PRESERVATION_DISTRICTS = "important-preservation-districts-for-groups-of-traditional-buildings"


def with_treasure_class(value: str) -> str:
    """102 の詳細ページの「国宝・重文区分」だけを差し替える。"""
    html = fixture("detail_102.html")
    assert html.count(TREASURE_CLASS_CELL) == 1
    return html.replace(TREASURE_CLASS_CELL, f"\t\t\t\t{value}\t\t\t</td>")


def without_kinds(html: str) -> str:
    """401 の詳細ページの主情報から種別１・種別２の値だけを抜く。

    措置の履歴 (下部のモーダル) にも同じ語が出るので、主情報の欄だけを空にする。
    """
    for cell in ("\t\t\t\t特別名勝\t\t\t</td>", "\t\t\t\t特別史跡\t\t\t</td>"):
        assert html.count(cell) == 1
        html = html.replace(cell, "\t\t\t\t\t\t\t</td>")
    return html


def caches(cache_dir: Path) -> tuple[LedgerCache, DetailCache]:
    """3 分類ぶん、実データを切り出した詳細ページ 1 枚ずつを持つキャッシュ。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    for category, (managed_id, name, area_name) in SAMPLES.items():
        put_ledger(ledger, category, area_named(area_name), [managed_id])
        put_detail(detail, category, managed_id, fixture(name))
    return ledger, detail


def test_種別ごとのリポジトリの都道府県ファイルへ書く(cache_dir: Path, tmp_path: Path) -> None:
    """ADR 0009 の配置。出力ディレクトリはリポジトリを並べた親。"""
    ledger, detail = caches(cache_dir)
    out = tmp_path / "repos"

    report = build_dataset(ledger, detail, list(SAMPLES), output_dir=out)

    assert report.built == 3
    assert sorted(path.relative_to(out).as_posix() for path in out.rglob("*.jsonl")) == [
        f"{PRESERVATION_DISTRICTS}/data/26_kyoto.jsonl",
        "national-treasures/data/29_nara.jsonl",
        "registered-tangible-cultural-properties/data/13_tokyo.jsonl",
    ]
    assert lines(out / "national-treasures/data/29_nara.jsonl")[0]["name"] == "石上神宮拝殿"


def test_102は国宝と重要文化財でリポジトリが分かれる(cache_dir: Path, tmp_path: Path) -> None:
    """振り分けは詳細ページの国宝・重文区分で決まる (ADR 0009)。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594", "2485"])
    put_detail(detail, DESIGNATED, "2594", with_treasure_class("国宝"))
    put_detail(detail, DESIGNATED, "2485", with_treasure_class("重要文化財"))
    out = tmp_path / "repos"

    report = build_dataset(ledger, detail, [DESIGNATED], output_dir=out)

    assert report.built == 2
    assert not report.missing_kind
    treasures = lines(out / "national-treasures/data/29_nara.jsonl")
    importants = lines(out / "important-cultural-properties/data/29_nara.jsonl")
    assert [record["managed_id"] for record in treasures] == ["2594"]
    assert [record["managed_id"] for record in importants] == ["2485"]
    # 由来の分類は行に残る。リポジトリが分かれても 1 行から読める
    assert treasures[0]["ledger_id"] == importants[0]["ledger_id"] == "102"


def test_国宝重文区分が読めない棟は重要文化財として扱い報告する(
    cache_dir: Path, tmp_path: Path
) -> None:
    """黙って振り分けると、区分の読み落としに気付けない (ADR 0009)。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594"])
    put_detail(detail, DESIGNATED, "2594", with_treasure_class(""))
    out = tmp_path / "repos"

    report = build_dataset(ledger, detail, [DESIGNATED], output_dir=out)

    assert report.missing_kind == 1
    assert report.has_anomalies
    assert (out / "important-cultural-properties/data/29_nara.jsonl").exists()
    assert not (out / "national-treasures").exists()
    assert "区分が読めず受け皿のリポジトリへ送った: 1 件" in format_report(report)


def test_401の複合指定は両方のリポジトリへ書く(cache_dir: Path, tmp_path: Path) -> None:
    """種別を 2 つ持つ指定は、どちらの種別の一覧から見ても構成員 (ADR 0012)。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, MONUMENTS, area_named("東京都"), ["712"])
    put_detail(detail, MONUMENTS, "712", fixture("detail_401.html"))
    out = tmp_path / "repos"

    report = build_dataset(ledger, detail, [MONUMENTS], output_dir=out)

    # 1 件を 2 箇所へ書くが、組み立てたレコードは 1 つ
    assert report.built == 1
    assert not report.unroutable
    special_scenic = lines(out / "special-places-of-scenic-beauty/data/13_tokyo.jsonl")
    special_historic = lines(out / "special-historic-sites/data/13_tokyo.jsonl")
    assert special_scenic == special_historic
    assert special_scenic[0]["name"] == "旧浜離宮庭園"
    assert special_scenic[0]["types"] == ["特別名勝", "特別史跡"]
    # 特別指定は通常の種別と排他 (名勝・史跡側には出さない)
    assert not (out / "places-of-scenic-beauty").exists()
    assert not (out / "historic-sites").exists()


def test_401で種別が読めない行はどこへも書かず報せる(cache_dir: Path, tmp_path: Path) -> None:
    """401 に受け皿は無い。黙って史跡へ送ると振り分けの誤りに気付けない (ADR 0012)。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, MONUMENTS, area_named("東京都"), ["712"])
    put_detail(detail, MONUMENTS, "712", without_kinds(fixture("detail_401.html")))
    out = tmp_path / "repos"

    report = build_dataset(ledger, detail, [MONUMENTS], output_dir=out)

    assert report.built == 1
    assert report.unroutable == ["401/712 旧浜離宮庭園"]
    assert report.has_anomalies
    assert not list(out.rglob("*.jsonl"))
    assert "種別が読めず書き先が無かった: 1 件" in format_report(report)


def test_行はキーで安定ソートする(cache_dir: Path, tmp_path: Path) -> None:
    """取得順に依存させない (ADR 0004)。並びが揺れると差分が読めなくなる。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594", "0001", "10"])
    for managed_id in ("2594", "0001", "10"):
        put_detail(detail, DESIGNATED, managed_id, fixture("detail_102.html"))

    build_dataset(ledger, detail, [DESIGNATED], output_dir=tmp_path / "repos")

    written = lines(tmp_path / "repos/national-treasures/data/29_nara.jsonl")
    assert [record["managed_id"] for record in written] == ["0001", "10", "2594"]


def test_地域をまたぐ棟は一度だけ書く(cache_dir: Path, tmp_path: Path) -> None:
    """102 の琵琶湖疏水施設は滋賀県と京都府の両方の CSV に出る (#6 の実測)。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    for area_name in ("滋賀県", "京都府"):
        put_ledger(ledger, DESIGNATED, area_named(area_name), ["2594"])
    put_detail(detail, DESIGNATED, "2594", fixture("detail_102.html"))

    report = build_dataset(ledger, detail, [DESIGNATED], output_dir=tmp_path / "repos")

    assert (report.total, report.built) == (1, 1)
    # 置き場は検索した地域ではなく、詳細ページの所在都道府県で決まる
    written = lines(tmp_path / "repos/national-treasures/data/29_nara.jsonl")
    assert written[0]["managed_id"] == "2594"


def test_詳細が未取得の行は数えて飛ばす(cache_dir: Path, tmp_path: Path) -> None:
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594", "9999"])
    put_detail(detail, DESIGNATED, "2594", fixture("detail_102.html"))

    report = build_dataset(ledger, detail, [DESIGNATED], output_dir=tmp_path / "repos")

    assert (report.total, report.built, report.missing_html) == (2, 1, 1)
    assert "未取得ぶんは fetch-detail で取れる" in format_report(report)


def test_読めないページがあっても残りは書く(cache_dir: Path, tmp_path: Path) -> None:
    """2 万件のうち 1 件で全体が止まると、直すまで何も出力できない。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594", "9999"])
    put_detail(detail, DESIGNATED, "2594", fixture("detail_102.html"))
    put_detail(detail, DESIGNATED, "9999", "<html><body>ただいま混み合っています</body></html>")

    report = build_dataset(ledger, detail, [DESIGNATED], output_dir=tmp_path / "repos")

    assert report.built == 1
    assert len(report.parse_failures) == 1
    assert report.parse_failures[0].startswith("102/9999")
    assert report.has_anomalies


def test_今回書かなかった既存ファイルを報せる(cache_dir: Path, tmp_path: Path) -> None:
    """都道府県の振り分けが変わると、古いファイルに行が残り続ける。"""
    ledger, detail = caches(cache_dir)
    out = tmp_path / "repos"
    stale = out / "national-treasures" / "data" / "01_hokkaido.jsonl"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}\n", encoding="utf-8")

    report = build_dataset(ledger, detail, list(SAMPLES), output_dir=out)

    assert report.stale_files == [str(stale)]
    assert stale.exists()  # 消しはしない (地域を絞った実行と区別できない)
    assert "今回書かなかった既存ファイル" in format_report(report)


def test_台帳が空なら何も書かない(cache_dir: Path, tmp_path: Path) -> None:
    report = build_dataset(
        LedgerCache(cache_dir), DetailCache(cache_dir), list(SAMPLES), output_dir=tmp_path / "repos"
    )

    assert (report.total, report.built, report.files) == (0, 0, [])
    assert not (tmp_path / "repos").exists()


class Test前回の出力を使い回す:
    """差分更新 (ADR 0018)。詳細を取り直していない行は前回の出力をそのまま使う。"""

    def snapshot(self, out: Path) -> dict[str, bytes]:
        return {
            path.relative_to(out).as_posix(): path.read_bytes()
            for path in sorted(out.rglob("*"))
            if path.is_file()
        }

    def test_取り直していない行が埋まり出力は1バイトも変わらない(
        self, cache_dir: Path, tmp_path: Path
    ) -> None:
        """生成物が決定的でないと、データが変わらない月にも差分が立つ (ADR 0014)。"""
        ledger, detail = caches(cache_dir)
        out = tmp_path / "repos"
        build_dataset(ledger, detail, list(SAMPLES), output_dir=out)
        before = self.snapshot(out)
        existing = read_existing(out, datasets_for(list(SAMPLES)))

        # 詳細を 1 枚も持たないキャッシュ = 今月は 1 件も取り直さなかった状態
        report = build_dataset(
            ledger,
            DetailCache(tmp_path / "empty"),
            list(SAMPLES),
            output_dir=out,
            reuse=Reuse(
                records=existing.records, labels=existing.labels, accessed_at=FETCHED_AT
            ),
        )

        assert (report.built, report.reused, report.missing_html) == (3, 3, 0)
        assert self.snapshot(out) == before
        assert "うち前回の出力をそのまま使った: 3 件" in format_report(report)

    def test_キャッシュにあれば取り直したぶんが勝つ(self, cache_dir: Path, tmp_path: Path) -> None:
        """取り直したのに前回の値が残ったら、差分更新の意味が無い。"""
        ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
        put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594"])
        put_detail(detail, DESIGNATED, "2594", fixture("detail_102.html"))
        out = tmp_path / "repos"
        stale = {"ledger_id": "102", "managed_id": "2594", "name": "古い名前"}

        build_dataset(
            ledger,
            detail,
            [DESIGNATED],
            output_dir=out,
            reuse=Reuse(records={"102/2594": stale}),
        )

        written = lines(out / "national-treasures/data/29_nara.jsonl")
        assert written[0]["name"] == "石上神宮拝殿"

    def test_台帳に出なかった行も残せる(self, cache_dir: Path, tmp_path: Path) -> None:
        """網羅性を確かめられない分類では、消えた指定を落とさずに残す (ADR 0018)。"""
        ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
        put_ledger(ledger, SELECTED, area_named("京都府"), ["16"])
        put_detail(detail, SELECTED, "16", fixture("detail_103.html"))
        gone: dict[str, Any] = {
            "ledger_id": "103",
            "managed_id": "9999",
            "name": "台帳から消えた地区",
            "prefecture": "京都府",
            "address": "京都市",
        }

        report = build_dataset(
            ledger,
            detail,
            [SELECTED],
            output_dir=tmp_path / "repos",
            reuse=Reuse(retained=[gone]),
        )

        written = lines(tmp_path / f"repos/{PRESERVATION_DISTRICTS}/data/26_kyoto.jsonl")
        assert [record["managed_id"] for record in written] == ["16", "9999"]
        assert report.retained == 1
        assert "台帳に出なかったが残した: 1 件" in format_report(report)

    def test_利用日は実行日になり表示名は前回から引き継ぐ(
        self, cache_dir: Path, tmp_path: Path
    ) -> None:
        """差分更新では既存の行の取得日を知る術がない (ADR 0018)。

        表示名も同じ理由で前回のぶんを土台にする — 毎月 1/12 しか組み立てないので、
        今月ぶんだけで作ると ``meta.json`` が月ごとに揺れる。
        """
        ledger, detail = caches(cache_dir)
        out = tmp_path / "repos"
        build_dataset(ledger, detail, list(SAMPLES), output_dir=out)
        existing = read_existing(out, datasets_for(list(SAMPLES)))

        build_dataset(
            ledger,
            DetailCache(tmp_path / "empty"),
            list(SAMPLES),
            output_dir=out,
            reuse=Reuse(
                records=existing.records,
                labels=existing.labels,
                # UTC の 15 時は日本時間の翌日。利用日は日本時間で切る (ADR 0014)
                accessed_at="2026-09-30T15:00:00+00:00",
            ),
        )

        meta = json.loads(
            (out / "national-treasures/meta.json").read_text(encoding="utf-8")
        )
        assert meta["source"]["accessed_date"] == "2026-10-01"
        assert "2026年10月1日に利用" in meta["source"]["attribution"]
        assert meta["labels"]["name"] == "名称"


def test_報告に件数と異常が出る(cache_dir: Path, tmp_path: Path) -> None:
    ledger, detail = caches(cache_dir)

    report = build_dataset(ledger, detail, list(SAMPLES), output_dir=tmp_path / "repos")
    text = format_report(report)

    assert "対象 3 件 / 出力 3 件" in text
    assert f"{PRESERVATION_DISTRICTS}/data/26_kyoto.jsonl (1 行)" in text
    # 103 には所在都道府県の欄が無いので、所在地から決まる
    assert "都道府県を所在地から決めた: 1 件" in text
