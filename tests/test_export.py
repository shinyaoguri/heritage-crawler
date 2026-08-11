"""出力層のテスト。

**外部サイトへは出ない。** 台帳 CSV と生 HTML のキャッシュを ``tmp_path`` に
組み立て、そこから JSON Lines を書かせる。ここで守りたいのは ADR 0004 の
「差分が行単位で読める」— 置き場と並び順、そして 1 件の失敗で全体を止めないこと。
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import fixture, make_csv, make_row
from heritage_crawler.cache import DetailCache, DetailEntry, LedgerCache, LedgerEntry
from heritage_crawler.catalog import BUILDING_CATEGORIES, SEARCH_AREAS, Area, Category
from heritage_crawler.export import build_dataset, format_report

REGISTERED, DESIGNATED, SELECTED = BUILDING_CATEGORIES  # 101 / 102 / 103

SAMPLES = {
    REGISTERED: ("00004339", "detail_101.html", "東京都"),
    DESIGNATED: ("2594", "detail_102.html", "奈良県"),
    SELECTED: ("16", "detail_103.html", "京都府"),
}


def area_named(name: str) -> Area:
    return next(area for area in SEARCH_AREAS if area.name == name)


def put_ledger(cache: LedgerCache, category: Category, area: Area, ids: list[str]) -> None:
    rows = [make_row({"台帳ID": category.code, "管理対象ID": managed_id}) for managed_id in ids]
    cache.record(
        category,
        area,
        LedgerEntry(
            category_code=category.code,
            area_name=area.name,
            hit_count=len(rows),
            row_count=len(rows),
            byte_count=0,
            fetched_at="2026-08-12T00:00:00+00:00",
        ),
        make_csv(rows),
    )


def put_detail(cache: DetailCache, category: Category, managed_id: str, html: str) -> None:
    cache.record(
        DetailEntry(
            daichou_id=category.code,
            kanri_taishou_id=managed_id,
            ok=True,
            byte_count=len(html),
            fetched_at="2026-08-12T00:00:00+00:00",
        ),
        html.encode("utf-8"),
    )


def caches(cache_dir: Path) -> tuple[LedgerCache, DetailCache]:
    """3 分類ぶん、実データを切り出した詳細ページ 1 枚ずつを持つキャッシュ。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    for category, (managed_id, name, area_name) in SAMPLES.items():
        put_ledger(ledger, category, area_named(area_name), [managed_id])
        put_detail(detail, category, managed_id, fixture(name))
    return ledger, detail


def lines(path: Path) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_分類と都道府県ごとのファイルへ書く(cache_dir: Path, tmp_path: Path) -> None:
    ledger, detail = caches(cache_dir)
    out = tmp_path / "data"

    report = build_dataset(ledger, detail, list(SAMPLES), output_dir=out)

    assert report.built == 3
    assert sorted(path.relative_to(out).as_posix() for path in out.rglob("*.jsonl")) == [
        "101/13_tokyo.jsonl",
        "102/29_nara.jsonl",
        "103/26_kyoto.jsonl",
    ]
    assert lines(out / "102/29_nara.jsonl")[0]["name"] == "石上神宮拝殿"


def test_行はキーで安定ソートする(cache_dir: Path, tmp_path: Path) -> None:
    """取得順に依存させない (ADR 0004)。並びが揺れると差分が読めなくなる。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594", "0001", "10"])
    for managed_id in ("2594", "0001", "10"):
        put_detail(detail, DESIGNATED, managed_id, fixture("detail_102.html"))

    build_dataset(ledger, detail, [DESIGNATED], output_dir=tmp_path / "data")

    written = lines(tmp_path / "data/102/29_nara.jsonl")
    assert [record["managed_id"] for record in written] == ["0001", "10", "2594"]


def test_地域をまたぐ棟は一度だけ書く(cache_dir: Path, tmp_path: Path) -> None:
    """102 の琵琶湖疏水施設は滋賀県と京都府の両方の CSV に出る (#6 の実測)。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    for area_name in ("滋賀県", "京都府"):
        put_ledger(ledger, DESIGNATED, area_named(area_name), ["2594"])
    put_detail(detail, DESIGNATED, "2594", fixture("detail_102.html"))

    report = build_dataset(ledger, detail, [DESIGNATED], output_dir=tmp_path / "data")

    assert (report.total, report.built) == (1, 1)
    # 置き場は検索した地域ではなく、詳細ページの所在都道府県で決まる
    assert lines(tmp_path / "data/102/29_nara.jsonl")[0]["managed_id"] == "2594"


def test_詳細が未取得の行は数えて飛ばす(cache_dir: Path, tmp_path: Path) -> None:
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594", "9999"])
    put_detail(detail, DESIGNATED, "2594", fixture("detail_102.html"))

    report = build_dataset(ledger, detail, [DESIGNATED], output_dir=tmp_path / "data")

    assert (report.total, report.built, report.missing_html) == (2, 1, 1)
    assert "未取得ぶんは fetch-detail で取れる" in format_report(report)


def test_読めないページがあっても残りは書く(cache_dir: Path, tmp_path: Path) -> None:
    """2 万件のうち 1 件で全体が止まると、直すまで何も出力できない。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594", "9999"])
    put_detail(detail, DESIGNATED, "2594", fixture("detail_102.html"))
    put_detail(detail, DESIGNATED, "9999", "<html><body>ただいま混み合っています</body></html>")

    report = build_dataset(ledger, detail, [DESIGNATED], output_dir=tmp_path / "data")

    assert report.built == 1
    assert len(report.parse_failures) == 1
    assert report.parse_failures[0].startswith("102/9999")
    assert report.has_anomalies


def test_今回書かなかった既存ファイルを報せる(cache_dir: Path, tmp_path: Path) -> None:
    """都道府県の振り分けが変わると、古いファイルに行が残り続ける。"""
    ledger, detail = caches(cache_dir)
    out = tmp_path / "data"
    stale = out / "102" / "01_hokkaido.jsonl"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}\n", encoding="utf-8")

    report = build_dataset(ledger, detail, list(SAMPLES), output_dir=out)

    assert report.stale_files == [str(stale)]
    assert stale.exists()  # 消しはしない (地域を絞った実行と区別できない)
    assert "今回書かなかった既存ファイル" in format_report(report)


def test_台帳が空なら何も書かない(cache_dir: Path, tmp_path: Path) -> None:
    report = build_dataset(
        LedgerCache(cache_dir), DetailCache(cache_dir), list(SAMPLES), output_dir=tmp_path / "data"
    )

    assert (report.total, report.built, report.files) == (0, 0, [])
    assert not (tmp_path / "data").exists()


def test_報告に件数と異常が出る(cache_dir: Path, tmp_path: Path) -> None:
    ledger, detail = caches(cache_dir)

    report = build_dataset(ledger, detail, list(SAMPLES), output_dir=tmp_path / "data")
    text = format_report(report)

    assert "対象 3 件 / 出力 3 件" in text
    assert "103/26_kyoto.jsonl (1 行)" in text
    # 103 には所在都道府県の欄が無いので、所在地から決まる
    assert "都道府県を所在地から決めた: 1 件" in text
