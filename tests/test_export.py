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
from heritage_crawler.export import REMOVED_FILENAME, Reuse, build_dataset, format_report
from heritage_crawler.ledger import read_ledger_rows
from heritage_crawler.update import plan_update, read_existing, reuse_for

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


def put_stale(out: Path, repo: str, name: str = "01_hokkaido.jsonl") -> Path:
    """前回はあったが、今回 1 件も書かない県のファイルを置く (#57)。"""
    path = out / repo / "data" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ledger_id": "102", "managed_id": "9998"}\n', encoding="utf-8")
    return path


class Test0件になったファイル:
    """行が全部無くなった県のファイルは書き直されない (#57)。

    地域を絞った実行と区別が付かないと消せないが、区別さえ付けば残す理由は無い —
    **0 件の県にはそもそもファイルが無い**のが出力の形 (ADR 0009 / ADR 0013)。
    """

    def test_全域の実行なら消す(self, cache_dir: Path, tmp_path: Path) -> None:
        ledger, detail = caches(cache_dir)
        out = tmp_path / "repos"
        stale = put_stale(out, "national-treasures")

        report = build_dataset(ledger, detail, list(SAMPLES), output_dir=out)

        assert not stale.exists()
        assert report.removed_files == [str(stale)]
        assert not report.stale_files
        # meta.json は書き出したぶんだけを列挙するので、消えたぶんは自然に外れる
        meta = json.loads((out / "national-treasures/meta.json").read_text(encoding="utf-8"))
        assert [entry["path"] for entry in meta["files"]] == ["data/29_nara.jsonl"]
        assert "0 件になったので消した" in format_report(report)

    def test_地域を絞った実行では消さず報せるだけ(self, cache_dir: Path, tmp_path: Path) -> None:
        """その地域を見ていないだけの県と区別が付かない。"""
        ledger, detail = caches(cache_dir)
        out = tmp_path / "repos"
        stale = put_stale(out, "national-treasures")

        report = build_dataset(
            ledger, detail, list(SAMPLES), areas=[area_named("奈良県")], output_dir=out
        )

        assert stale.exists()
        assert str(stale) in report.stale_files
        assert not report.removed_files
        assert "今回書かなかった既存ファイル" in format_report(report)

    def test_詳細が未取得の行があるときは消さない(self, cache_dir: Path, tmp_path: Path) -> None:
        """行が落ちたのは指定が消えたからではなく、取れていないから。"""
        ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
        put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594", "9999"])
        put_detail(detail, DESIGNATED, "2594", fixture("detail_102.html"))
        out = tmp_path / "repos"
        stale = put_stale(out, "national-treasures")

        report = build_dataset(ledger, detail, [DESIGNATED], output_dir=out)

        assert report.missing_html == 1
        assert stale.exists()
        assert report.stale_files == [str(stale)]

    def test_読めないページがあるときは消さない(self, cache_dir: Path, tmp_path: Path) -> None:
        """200 で返るエラーページを掴んだ回も、0 件を断言できない (ADR 0011)。"""
        ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
        put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594", "9999"])
        put_detail(detail, DESIGNATED, "2594", fixture("detail_102.html"))
        put_detail(detail, DESIGNATED, "9999", "<html><body>ただいま混み合っています</body></html>")
        out = tmp_path / "repos"
        stale = put_stale(out, "national-treasures")

        report = build_dataset(ledger, detail, [DESIGNATED], output_dir=out)

        assert report.parse_failures
        assert stale.exists()
        assert report.stale_files == [str(stale)]

    def test_その種別に1件も書かなかったときは消さない(
        self, cache_dir: Path, tmp_path: Path
    ) -> None:
        """``fetch-ledger`` が途中で止まった回に、リポジトリを全滅させないため。

        台帳に行が無ければ ``missing_html`` も増えないので、取りこぼしの検査では
        捕まらない。
        """
        ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
        put_ledger(ledger, SELECTED, area_named("京都府"), ["16"])
        put_detail(detail, SELECTED, "16", fixture("detail_103.html"))
        out = tmp_path / "repos"
        stale = put_stale(out, "national-treasures", "29_nara.jsonl")

        report = build_dataset(ledger, detail, [DESIGNATED, SELECTED], output_dir=out)

        assert report.missing_html == 0
        assert stale.exists()
        assert report.stale_files == [str(stale)]

    def test_消した種別の利用日は進む(self, cache_dir: Path, tmp_path: Path) -> None:
        """削除しか起きなかった週も、行は動いている (ADR 0020 の据え置き)。"""
        ledger, detail = caches(cache_dir)
        out = tmp_path / "repos"
        build_dataset(ledger, detail, list(SAMPLES), output_dir=out)
        existing = read_existing(out, datasets_for(list(SAMPLES)))
        # 前回の出力を読んだ後に置く = 台帳にも前回のレコードにも無い県のファイル
        stale = put_stale(out, "national-treasures")

        build_dataset(
            ledger,
            DetailCache(tmp_path / "empty"),
            list(SAMPLES),
            output_dir=out,
            reuse=Reuse(
                records=existing.records,
                labels=existing.labels,
                accessed_at="2026-12-25T00:00:00+00:00",
                accessed_dates=existing.accessed_dates,
            ),
        )

        def accessed(repo: str) -> str:
            meta = json.loads((out / repo / "meta.json").read_text(encoding="utf-8"))
            return str(meta["source"]["accessed_date"])

        assert not stale.exists()
        assert accessed("national-treasures") == "2026-12-25"
        # 消えたファイルが無い種別は据え置き
        assert accessed("registered-tangible-cultural-properties") == "2026-08-12"


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

    def test_行が動かなければ利用日を据え置く(self, cache_dir: Path, tmp_path: Path) -> None:
        """確認しただけの回にコミットを立てない (ADR 0020)。

        利用日は「そのデータを取り出した日」なので、取り出し直していない回に
        動かす理由が無い。据え置けば `meta.json` も 1 バイトも変わらない。
        """
        ledger, detail = caches(cache_dir)
        out = tmp_path / "repos"
        build_dataset(ledger, detail, list(SAMPLES), output_dir=out)
        before = self.snapshot(out)
        existing = read_existing(out, datasets_for(list(SAMPLES)))

        build_dataset(
            ledger,
            DetailCache(tmp_path / "empty"),
            list(SAMPLES),
            output_dir=out,
            reuse=Reuse(
                records=existing.records,
                labels=existing.labels,
                # ずっと後の回に走らせても、行が動かなければ日付は進まない。
                accessed_at="2026-12-25T00:00:00+00:00",
                accessed_dates=existing.accessed_dates,
            ),
        )

        assert self.snapshot(out) == before

    def test_行が動いた種別だけ利用日が進む(self, cache_dir: Path, tmp_path: Path) -> None:
        """取り直したぶんがあるなら、その種別の利用日は実行日になる。"""
        ledger, detail = caches(cache_dir)
        out = tmp_path / "repos"
        build_dataset(ledger, detail, list(SAMPLES), output_dir=out)
        existing = read_existing(out, datasets_for(list(SAMPLES)))
        # 102 の 1 行だけ違う値にする。詳細キャッシュを外すので、この値がそのまま
        # 書き出されて前回と食い違う = その種別の行が動いた状態になる。
        changed = dict(existing.records["102/2594"], name="別の名前")

        build_dataset(
            ledger,
            DetailCache(tmp_path / "empty"),
            list(SAMPLES),
            output_dir=out,
            reuse=Reuse(
                records={**existing.records, "102/2594": changed},
                labels=existing.labels,
                accessed_at="2026-12-25T00:00:00+00:00",
                accessed_dates=existing.accessed_dates,
            ),
        )

        def accessed(repo: str) -> str:
            meta = json.loads((out / repo / "meta.json").read_text(encoding="utf-8"))
            return str(meta["source"]["accessed_date"])

        assert accessed("national-treasures") == "2026-12-25"
        # 動いていない種別は据え置き。**動いたぶんだけコミットが立つ。**
        assert accessed("registered-tangible-cultural-properties") == "2026-08-12"

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


class Test削除の記録:
    """落とした行を `removed.jsonl` に残す (ADR 0021)。

    状態型なので「いま消えているもの」しか並ばない — 復活すれば行は消える。
    誤検出が「解除された文化財」として固定されないための性質。
    """

    def removed(self, out: Path, repo: str) -> list[dict[str, Any]]:
        return lines(out / repo / REMOVED_FILENAME)

    def prepare(self, cache_dir: Path, out: Path, ids: list[str]) -> DetailCache:
        """103 を `ids` ぶん書き出した状態にする。"""
        detail = DetailCache(cache_dir)
        ledger = LedgerCache(cache_dir / "before")
        put_ledger(ledger, SELECTED, area_named("京都府"), ids)
        for managed_id in ids:
            put_detail(detail, SELECTED, managed_id, fixture("detail_103.html"))
        build_dataset(ledger, detail, [SELECTED], output_dir=out)
        return detail

    def rebuild(
        self, cache_dir: Path, detail: DetailCache, out: Path, ids: list[str]
    ) -> Any:
        """台帳を `ids` だけに減らして組み立て直す (差分更新と同じ経路を通す)。"""
        ledger = LedgerCache(cache_dir / "after")
        put_ledger(ledger, SELECTED, area_named("京都府"), ids)
        existing = read_existing(out, datasets_for([SELECTED]))
        plan = plan_update(
            existing,
            read_ledger_rows(ledger, [SELECTED]),
            slot=99,
            complete_categories={SELECTED.code},
        )
        reuse = reuse_for(plan, existing, "2026-08-17T00:00:00+00:00")
        return build_dataset(ledger, detail, [SELECTED], output_dir=out, reuse=reuse)

    def test_落とした行が前回居たリポジトリに入る(self, cache_dir: Path, tmp_path: Path) -> None:
        out = tmp_path / "repos"
        detail = self.prepare(cache_dir, out, ["16", "17"])

        report = self.rebuild(cache_dir, detail, out, ["16"])

        entry = self.removed(out, PRESERVATION_DISTRICTS)
        assert [item["managed_id"] for item in entry] == ["17"]
        assert entry[0]["conclusion"] == "unverified"
        assert entry[0]["missing_since"] == "2026-08-17"
        # レコードを丸ごと残す — 番号だけでは「何が消えたか」が分からない。
        assert entry[0]["name"] == "京都市上賀茂"
        assert report.removed_records == 1
        assert "落とした行を記録した: 1 件" in format_report(report)

    def test_消えたままの回は記録が1バイトも動かない(
        self, cache_dir: Path, tmp_path: Path
    ) -> None:
        """`missing_since` は消えたと最初に気付いた日。翌週に進めては意味が変わる。"""
        out = tmp_path / "repos"
        detail = self.prepare(cache_dir, out, ["16", "17"])
        self.rebuild(cache_dir, detail, out, ["16"])
        before = (out / PRESERVATION_DISTRICTS / REMOVED_FILENAME).read_bytes()

        self.rebuild(cache_dir, detail, out, ["16"])

        assert (out / PRESERVATION_DISTRICTS / REMOVED_FILENAME).read_bytes() == before

    def test_復活すると記録から消える(self, cache_dir: Path, tmp_path: Path) -> None:
        """状態型なので、台帳へ戻った行は記録から落ちる。0 件ならファイルごと消す。"""
        out = tmp_path / "repos"
        detail = self.prepare(cache_dir, out, ["16", "17"])
        self.rebuild(cache_dir, detail, out, ["16"])

        report = self.rebuild(cache_dir, detail, out, ["16", "17"])

        assert not (out / PRESERVATION_DISTRICTS / REMOVED_FILENAME).exists()
        assert report.restored_records == 1

    def test_書き先が変わると移動元にだけ入る(self, cache_dir: Path, tmp_path: Path) -> None:
        """台帳には居るのに振り分けが変わった行 (102 の区分変更、401 の種別変更)。

        利用者から見れば削除と区別が付かないので、移動元の記録に残す。
        """
        out = tmp_path / "repos"
        ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
        put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594"])
        put_detail(detail, DESIGNATED, "2594", with_treasure_class("国宝"))
        build_dataset(ledger, detail, [DESIGNATED], output_dir=out)

        put_detail(detail, DESIGNATED, "2594", with_treasure_class("重要文化財"))
        existing = read_existing(out, datasets_for([DESIGNATED]))
        plan = plan_update(
            existing,
            read_ledger_rows(ledger, [DESIGNATED]),
            slot=99,
            complete_categories={DESIGNATED.code},
        )
        build_dataset(
            ledger,
            detail,
            [DESIGNATED],
            output_dir=out,
            reuse=reuse_for(plan, existing, "2026-08-17T00:00:00+00:00"),
        )

        entry = self.removed(out, "national-treasures")
        assert [item["managed_id"] for item in entry] == ["2594"]
        assert entry[0]["conclusion"] == "rerouted"
        assert entry[0]["evidence"]["absent_from_ledger"] is False
        assert entry[0]["evidence"]["matched_elsewhere"] == ["important-cultural-properties"]
        assert not (out / "important-cultural-properties" / REMOVED_FILENAME).exists()

    def test_全件の組み立て直しでは触らない(self, cache_dir: Path, tmp_path: Path) -> None:
        """`build-records` はキャッシュしか読まないので、履歴を再現できない。

        触らないと決めておかないと、スキーマ変更のたびに記録が静かに失われる
        (ADR 0021 の「影響」)。
        """
        ledger, detail = caches(cache_dir)
        out = tmp_path / "repos"
        build_dataset(ledger, detail, list(SAMPLES), output_dir=out)
        path = out / PRESERVATION_DISTRICTS / REMOVED_FILENAME
        path.write_text('{"ledger_id": "103", "managed_id": "9999"}\n', encoding="utf-8")
        before = path.read_bytes()

        build_dataset(ledger, detail, list(SAMPLES), output_dir=out)

        assert path.read_bytes() == before


def test_報告に件数と異常が出る(cache_dir: Path, tmp_path: Path) -> None:
    ledger, detail = caches(cache_dir)

    report = build_dataset(ledger, detail, list(SAMPLES), output_dir=tmp_path / "repos")
    text = format_report(report)

    assert "対象 3 件 / 出力 3 件" in text
    assert f"{PRESERVATION_DISTRICTS}/data/26_kyoto.jsonl (1 行)" in text
    # 103 には所在都道府県の欄が無いので、所在地から決まる
    assert "都道府県を所在地から決めた: 1 件" in text
