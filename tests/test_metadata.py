"""データセットのメタデータのテスト。

**外部サイトへは出ない。** キャッシュを ``tmp_path`` に組み立てて書かせる。

ここで守りたいのは ADR 0014 の 3 つ。

- 出典表記の利用日が**取得の実測**から出ること (組み立てた日ではない)
- 表示名が**分類ごとの原文ラベル**から出ること (対応表を持つのはクローラーだけ)
- 語彙と件数が**書き出した行と食い違わない**こと
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import area_named, fixture, lines, put_detail, put_ledger
from heritage_crawler.cache import DetailCache, LedgerCache
from heritage_crawler.catalog import DESIGNATED, MONUMENTS, REGISTERED, SELECTED
from heritage_crawler.export import build_dataset
from heritage_crawler.metadata import SCHEMA_VERSION

TREASURES = "national-treasures"
REGISTERED_REPO = "registered-tangible-cultural-properties"
PRESERVATION_DISTRICTS = "important-preservation-districts-for-groups-of-traditional-buildings"


def meta(out: Path, repo: str) -> dict[str, Any]:
    return json.loads((out / repo / "meta.json").read_text(encoding="utf-8"))


def build_one(
    cache_dir: Path,
    out: Path,
    category: Any = DESIGNATED,
    managed_ids: tuple[str, ...] = ("2594",),
    html: str = "detail_102.html",
    area_name: str = "奈良県",
    fetched_at: str | None = None,
    values: dict[str, str] | None = None,
) -> None:
    """1 分類ぶんを組み立てて書き出す。既定は国宝 (石上神宮拝殿) 1 件。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    kwargs = {"fetched_at": fetched_at} if fetched_at else {}
    put_ledger(ledger, category, area_named(area_name), managed_ids, values=values, **kwargs)
    for managed_id in managed_ids:
        put_detail(detail, category, managed_id, fixture(html), **kwargs)
    build_dataset(ledger, detail, [category], output_dir=out)


def test_メタデータはデータリポジトリのルートに出る(cache_dir: Path, tmp_path: Path) -> None:
    """規約上の義務はデータに付いているので、来歴もデータと一緒に置く (ADR 0014)。"""
    out = tmp_path / "repos"

    build_one(cache_dir, out)

    payload = meta(out, TREASURES)
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["dataset"] == {
        "repo": TREASURES,
        "name": "国宝（建造物）",
        "category": {"code": "102", "name": "国宝・重要文化財（建造物）"},
        "kinds": ["国宝"],
    }
    assert payload["generator"]["name"] == "heritage-crawler"


def test_利用日は詳細を取得した日を日本時間で切る(cache_dir: Path, tmp_path: Path) -> None:
    """UTC のまま日付を取ると、日本の夕方以降に取ったぶんが 1 日前にずれる。"""
    out = tmp_path / "repos"

    build_one(cache_dir, out, fetched_at="2026-08-11T15:29:02+00:00")

    assert meta(out, TREASURES)["source"]["accessed_date"] == "2026-08-12"


def test_利用日はデータセットで最も新しい取得日を採る(cache_dir: Path, tmp_path: Path) -> None:
    """取得は数時間かかる。表記に載る日付は 1 つなので、最後に触った日を採る。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594", "2485"])
    put_detail(detail, DESIGNATED, "2594", fixture("detail_102.html"), "2026-08-10T01:00:00+00:00")
    put_detail(detail, DESIGNATED, "2485", fixture("detail_102.html"), "2026-08-11T01:00:00+00:00")
    out = tmp_path / "repos"

    build_dataset(ledger, detail, [DESIGNATED], output_dir=out)

    assert meta(out, TREASURES)["source"]["accessed_date"] == "2026-08-11"


def test_出典表記は規約の書式で入る(cache_dir: Path, tmp_path: Path) -> None:
    """「上記を加工して作成」まで含めて 1 つの表示 (ADR 0007)。"""
    out = tmp_path / "repos"

    build_one(cache_dir, out, fetched_at="2026-08-11T15:29:02+00:00")

    source = meta(out, TREASURES)["source"]
    assert source["attribution"] == (
        "出典：「国指定文化財等データベース」（文化庁）\n"
        "（https://kunishitei.bunka.go.jp/）（2026年8月12日に利用）\n"
        "上記を加工して作成"
    )
    assert source["terms_url"] == "https://kunishitei.bunka.go.jp/top/policy"


def test_表示名は分類ごとの原文ラベルから決まる(cache_dir: Path, tmp_path: Path) -> None:
    """同じ ``designation_number`` でも、101 は登録番号・102 は指定番号と呼ぶ。

    どの呼び名が現れるかは対応表からは決まらないので、実測して載せる。
    """
    out = tmp_path / "repos"

    build_one(cache_dir, out)
    build_one(
        cache_dir,
        out,
        category=REGISTERED,
        managed_ids=("00004339",),
        html="detail_101.html",
        area_name="東京都",
    )

    assert meta(out, TREASURES)["labels"]["designation_number"] == "指定番号"
    assert meta(out, REGISTERED_REPO)["labels"]["designation_number"] == "登録番号"


def test_番号で分かれた欄の表示名は畳む(cache_dir: Path, tmp_path: Path) -> None:
    """101 の種別は種別１・種別２に分かれるが、寄る先のキーは 1 つ。"""
    out = tmp_path / "repos"

    build_one(
        cache_dir,
        out,
        category=REGISTERED,
        managed_ids=("00004339",),
        html="detail_101.html",
        area_name="東京都",
    )

    assert meta(out, REGISTERED_REPO)["labels"]["types"] == "種別"


def test_附指定や措置の中のキーは親の名前で修飾する(cache_dir: Path, tmp_path: Path) -> None:
    """入れ子の中身にも表示名が要る。平らなまま親のキーと混ざらないようにする。"""
    out = tmp_path / "repos"

    build_one(cache_dir, out)
    build_one(
        cache_dir,
        out,
        category=MONUMENTS,
        managed_ids=("712",),
        html="detail_401.html",
        area_name="東京都",
    )

    assert meta(out, TREASURES)["labels"]["annexes.name"] == "附名称"
    assert meta(out, "special-historic-sites")["labels"]["measures.date"] == "異動年月日"


def test_原文ラベルを持たないキーにも表示名が付く(cache_dir: Path, tmp_path: Path) -> None:
    """緯度経度・解説文・URL は対応表に無い。読む側が独自の和訳を持たずに済むように。"""
    out = tmp_path / "repos"

    build_one(cache_dir, out, values={"緯度": "34.59", "経度": "135.85"})

    labels = meta(out, TREASURES)["labels"]
    assert labels["latitude"] == "緯度"
    assert labels["url"] == "詳細ページ"
    assert labels["has_photo"] == "写真の有無"


def test_原文に欄の無いキーにも表示名が付く(cache_dir: Path, tmp_path: Path) -> None:
    """103 の詳細ページに所在都道府県の欄は無く、所在地から決めている。

    実測だけに頼ると、この分類でだけ表示名が欠ける。
    """
    out = tmp_path / "repos"

    build_one(
        cache_dir,
        out,
        category=SELECTED,
        managed_ids=("16",),
        html="detail_103.html",
        area_name="京都府",
    )

    payload = meta(out, PRESERVATION_DISTRICTS)
    assert payload["fields"]["prefecture"] == 1
    assert payload["labels"]["prefecture"] == "所在都道府県"


def test_ファセットは値のあるキーだけを件数の多い順に出す(
    cache_dir: Path, tmp_path: Path
) -> None:
    """語彙が空のキーまで並べると、読む側が空の絞り込みを描くことになる。"""
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, DESIGNATED, area_named("奈良県"), ["2594", "2485", "1"])
    for managed_id in ("2594", "2485", "1"):
        put_detail(detail, DESIGNATED, managed_id, fixture("detail_102.html"))
    out = tmp_path / "repos"

    build_dataset(ledger, detail, [DESIGNATED], output_dir=out)

    facets = meta(out, TREASURES)["facets"]
    assert facets["prefecture"] == {"奈良県": 3}
    assert facets["designation_kind"] == {"指定": 3}
    # 102 に特別区分は無い。空の語彙はキーごと出さない
    assert "special_class" not in facets


def test_件数は書き出した行と一致する(cache_dir: Path, tmp_path: Path) -> None:
    """数え直す場所を 1 つにして、出力と件数がずれる余地を作らない。"""
    out = tmp_path / "repos"

    build_one(
        cache_dir,
        out,
        managed_ids=("2594", "2485"),
        values={"緯度": "34.59", "経度": "135.85"},
    )

    payload = meta(out, TREASURES)
    written = lines(out / TREASURES / "data" / "29_nara.jsonl")
    assert payload["counts"] == {"records": len(written), "files": 1, "with_coordinates": 2}
    assert payload["files"] == [
        {"path": "data/29_nara.jsonl", "area_code": "29", "area_name": "奈良県", "records": 2}
    ]


def test_緯度経度の無い行は座標ありに数えない(cache_dir: Path, tmp_path: Path) -> None:
    """地図に載らない件数が分かるようにする。全件に座標があるとは限らない。"""
    out = tmp_path / "repos"

    build_one(cache_dir, out)

    assert meta(out, TREASURES)["counts"]["with_coordinates"] == 0


def test_片方しかない座標は座標ありに数えない(cache_dir: Path, tmp_path: Path) -> None:
    """緯度だけでは地図に置けない。読めなかった片方は空になって落ちる。"""
    out = tmp_path / "repos"

    build_one(cache_dir, out, values={"緯度": "34.59", "経度": "―"})

    written = lines(out / TREASURES / "data" / "29_nara.jsonl")[0]
    assert "latitude" in written and "longitude" not in written
    assert meta(out, TREASURES)["counts"]["with_coordinates"] == 0


def test_同じ入力なら同じバイト列を書く(cache_dir: Path, tmp_path: Path) -> None:
    """毎月書き換わると、データが変わっていない月にも差分が立つ。"""
    out = tmp_path / "repos"

    build_one(cache_dir, out)
    first = (out / TREASURES / "meta.json").read_bytes()
    build_one(cache_dir, out)

    assert (out / TREASURES / "meta.json").read_bytes() == first
