"""確認した日の記録のテスト (ADR 0023)。

**外部サイトへは出ない。** キャッシュを ``tmp_path`` に組み立てて書かせる。

守りたいのは 1 つ — **中身が動かない週も確認日が進むこと**。進まなければ、上流が
静かなことと週次が止まっていることを、データリポジトリを見るだけでは区別できない。

裏返しの取り決めも同じだけ大事で、``build-records`` (ローカルの全件組み立て) では
書かない。あれは相手先を見にいっていないうえ、日付が入ると生成物が実行日で揺れる。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import FETCHED_AT, area_named, fixture, put_detail, put_ledger
from heritage_crawler.cache import DetailCache, LedgerCache
from heritage_crawler.catalog import DESIGNATED, datasets_for
from heritage_crawler.export import Checked, Reuse, build_dataset
from heritage_crawler.status import SCHEMA_VERSION, STATUS_FILENAME, checked_today
from heritage_crawler.update import read_existing

TREASURES = "national-treasures"
"""フィクスチャの 102 は国宝 (石上神宮拝殿) なので、書き先はここになる。"""


def status(out: Path, repo: str = TREASURES) -> dict[str, Any]:
    return json.loads((out / repo / STATUS_FILENAME).read_text(encoding="utf-8"))


def snapshot(out: Path) -> dict[str, bytes]:
    """``status.json`` を除いた出力。**毎週動いてよいのはあれだけ**。"""
    return {
        path.relative_to(out).as_posix(): path.read_bytes()
        for path in sorted(out.rglob("*"))
        if path.is_file() and path.name != STATUS_FILENAME
    }


def caches(cache_dir: Path, managed_ids: tuple[str, ...] = ("2594",)) -> tuple[Any, Any]:
    ledger, detail = LedgerCache(cache_dir), DetailCache(cache_dir)
    put_ledger(ledger, DESIGNATED, area_named("奈良県"), managed_ids)
    for managed_id in managed_ids:
        put_detail(detail, DESIGNATED, managed_id, fixture("detail_102.html"))
    return ledger, detail


def test_確認日はデータリポジトリのルートに出る(cache_dir: Path, tmp_path: Path) -> None:
    """``meta.json`` と並べる。``data/`` の下に置くと配信側に弾かれる (ADR 0021)。"""
    ledger, detail = caches(cache_dir)
    out = tmp_path / "repos"

    build_dataset(ledger, detail, [DESIGNATED], output_dir=out, checked=Checked(date="2026-08-24"))

    assert status(out) == {
        "schema_version": SCHEMA_VERSION,
        "repo": TREASURES,
        "checked_date": "2026-08-24",
        "accessed_date": "2026-08-12",
        "changed": True,
        "records": 1,
    }


def test_確認日を渡さなければ書かない(cache_dir: Path, tmp_path: Path) -> None:
    """``build-records`` は相手先を見にいっていない。

    ここで書いてしまうと、キャッシュから組み立て直すたびに日付が動き、
    「同じ入力なら同じバイト列」(ADR 0014) も崩れる。
    """
    ledger, detail = caches(cache_dir)
    out = tmp_path / "repos"

    build_dataset(ledger, detail, [DESIGNATED], output_dir=out)

    assert (out / TREASURES / "meta.json").exists()
    assert not (out / TREASURES / STATUS_FILENAME).exists()


def test_行が動かない週も確認日は進む(cache_dir: Path, tmp_path: Path) -> None:
    """**このファイルの存在理由**。中身は据え置き、確認日だけが進む (ADR 0023)。"""
    ledger, detail = caches(cache_dir)
    out = tmp_path / "repos"
    build_dataset(ledger, detail, [DESIGNATED], output_dir=out, checked=Checked(date="2026-08-17"))
    before = snapshot(out)
    existing = read_existing(out, datasets_for([DESIGNATED]))

    # 詳細を 1 枚も持たないキャッシュ = 今週は 1 件も取り直さなかった状態
    build_dataset(
        ledger,
        DetailCache(tmp_path / "empty"),
        [DESIGNATED],
        output_dir=out,
        reuse=Reuse(
            records=existing.records,
            labels=existing.labels,
            accessed_at="2026-08-24T00:00:00+00:00",
            accessed_dates=existing.accessed_dates,
        ),
        checked=Checked(date="2026-08-24"),
    )

    assert snapshot(out) == before  # 行も meta.json も 1 バイトも動かない
    payload = status(out)
    assert payload["checked_date"] == "2026-08-24"
    assert payload["accessed_date"] == "2026-08-12"  # 利用日は据え置き
    assert payload["changed"] is False


def test_行が動いた週は変わったと記す(cache_dir: Path, tmp_path: Path) -> None:
    """利用日の据え置き判定と同じ ``touched`` を見る。読む側が差分を追わずに済む。"""
    out = tmp_path / "repos"
    ledger, detail = caches(cache_dir)
    build_dataset(ledger, detail, [DESIGNATED], output_dir=out, checked=Checked(date="2026-08-17"))

    ledger, detail = caches(cache_dir, ("2594", "2485"))  # 1 件増えた
    build_dataset(ledger, detail, [DESIGNATED], output_dir=out, checked=Checked(date="2026-08-24"))

    payload = status(out)
    assert payload["changed"] is True
    assert payload["records"] == 2


def test_実行のURLは渡したときだけ載る(cache_dir: Path, tmp_path: Path) -> None:
    """手元での組み立てには実行の URL が無い。空文字を残すと「あるはずのものが空」に読める。"""
    ledger, detail = caches(cache_dir)
    url = "https://github.com/shinyaoguri/heritage-crawler/actions/runs/1"

    local = tmp_path / "local"
    build_dataset(
        ledger, detail, [DESIGNATED], output_dir=local, checked=Checked(date="2026-08-24")
    )
    actions = tmp_path / "actions"
    build_dataset(
        ledger,
        detail,
        [DESIGNATED],
        output_dir=actions,
        checked=Checked(date="2026-08-24", run_url=url),
    )

    assert "run_url" not in status(local)
    assert status(actions)["run_url"] == url


def test_確認日は日本時間で切る() -> None:
    """巡回の枠も利用日も日本時間。ここだけ UTC だと日本の朝に走る週次が前日を書く。"""
    from datetime import UTC, datetime

    assert checked_today(datetime(2026, 8, 23, 18, 0, tzinfo=UTC)) == "2026-08-24"
    assert checked_today(datetime.fromisoformat(FETCHED_AT)) == "2026-08-12"
