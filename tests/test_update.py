"""差分更新層のテスト (ADR 0018)。

**外部サイトへは出ない。** 前回の出力は ``tmp_path`` に書いた JSON Lines で、
台帳はキャッシュに置いた CSV。ここで守りたいのは 3 つ。

- 取り直すのは新規・台帳の値が変わったぶん・巡回の 1/12 だけ
- 巡回の割り当てが実行のたびに変わらない (変わると毎月ちがう 1/12 を取る)
- **取りこぼしを指定解除と読み違えて行を消さない**
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from conftest import make_row
from heritage_crawler.catalog import DESIGNATED, MONUMENTS, REGISTERED, Category, datasets_for
from heritage_crawler.ledger import LedgerRow
from heritage_crawler.metadata import METADATA_FILENAME
from heritage_crawler.update import (
    ROTATION_MONTHS,
    Existing,
    Reason,
    UpdateError,
    format_plan,
    ledger_changes,
    plan_update,
    read_existing,
    reuse_for,
    rotation_slot,
)

ALL_CATEGORIES = {category.code for category in (REGISTERED, DESIGNATED, MONUMENTS)}


def row(category: Category, values: Mapping[str, str]) -> LedgerRow:
    """台帳の 1 行。指定しなかった列は空になる。"""
    return LedgerRow(
        category=category, values=tuple(make_row({"台帳ID": category.code, **values}))
    )


def record(**values: Any) -> dict[str, Any]:
    """前回の出力の 1 行。キーは ``record.KEY_ORDER`` のもの。"""
    return {"ledger_id": "101", "managed_id": "1", **values}


def write_records(
    output_dir: Path, repo: str, area_file: str, records: list[dict[str, Any]]
) -> Path:
    path = output_dir / repo / "data" / area_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8"
    )
    return path


class Test巡回の割り当て:
    def test_実行のたびに同じ枠へ入る(self) -> None:
        """組み込みの hash は実行ごとに種が変わるので使えない (毎月ちがう 1/12 を取ってしまう)。"""
        assert rotation_slot("401/00003452") == rotation_slot("401/00003452") == 1

    def test_枠は0から11(self) -> None:
        slots = {rotation_slot(f"101/{number}") for number in range(500)}
        assert slots == set(range(ROTATION_MONTHS))

    def test_キーが違えば枠も散る(self) -> None:
        """同じ台帳ID の連番が同じ枠に固まると、1 か月に負荷が寄る。"""
        slots = [rotation_slot(f"101/{number:08d}") for number in range(120)]
        assert max(slots.count(slot) for slot in set(slots)) < 30


class Test台帳との突き合わせ:
    def test_一致していれば変更なし(self) -> None:
        changed = ledger_changes(
            row(REGISTERED, {"名称": "宮下家住宅主屋", "所在地": "横浜市", "緯度": "35.4"}),
            record(name="宮下家住宅主屋", address="横浜市", latitude=35.4),
        )
        assert changed == []

    def test_空白だけの違いは変更としない(self) -> None:
        """CSV は全角、詳細ページは半角で同じ名称を書くことがある (実データで 137 件)。"""
        changed = ledger_changes(
            row(REGISTERED, {"名称": "高照神社　津軽信政公墓"}),
            record(name="高照神社 津軽信政公墓"),
        )
        assert changed == []

    def test_名称と座標の変更を見つける(self) -> None:
        changed = ledger_changes(
            row(REGISTERED, {"名称": "新しい名前", "緯度": "35.4", "経度": "139.5"}),
            record(name="古い名前", latitude=35.4, longitude=139.0),
        )
        assert changed == ["名称", "経度"]

    def test_座標が消えたことも見つける(self) -> None:
        changed = ledger_changes(row(REGISTERED, {"緯度": "35.4"}), record())
        assert changed == ["緯度"]

    def test_種別の増減は1つの欄として報せる(self) -> None:
        """種別1 / 種別2 はどちらが動いても「種別が変わった」以上の意味を持たない。"""
        changed = ledger_changes(
            row(MONUMENTS, {"種別1": "史跡", "種別2": "名勝"}), record(types=["史跡"])
        )
        assert changed == ["種別"]

    def test_102の種別1は国宝重文区分として比べる(self) -> None:
        """CSV の列は分類によって意味が変わる (CLAUDE.md)。

        取り違えると 102 の全件が毎月「変わった」ことになる。
        """
        unchanged = ledger_changes(
            row(DESIGNATED, {"種別1": "重要文化財", "種別2": "近代／学校"}),
            record(national_treasure_class="重要文化財", types=["近代／学校"]),
        )
        assert unchanged == []

        promoted = ledger_changes(
            row(DESIGNATED, {"種別1": "国宝", "種別2": "近代／学校"}),
            record(national_treasure_class="重要文化財", types=["近代／学校"]),
        )
        # 振り分け先リポジトリが変わる変更なので、翌月に取り直したい (ADR 0009)
        assert promoted == ["種別1"]

    def test_CSVが空の欄は比べない(self) -> None:
        """一覧から回収した行は所在地も種別も持たない (ADR 0017)。毎月「変わった」にしない。"""
        changed = ledger_changes(
            row(MONUMENTS, {"名称": "オオサンショウウオ生息地"}),
            record(name="オオサンショウウオ生息地", address="鳥取県", types=["天然記念物"]),
        )
        assert changed == []


class Test前回の出力の読み戻し:
    def test_全リポジトリの行と表示名を読む(self, tmp_path: Path) -> None:
        write_records(tmp_path, "historic-sites", "13_tokyo.jsonl", [record(ledger_id="401")])
        (tmp_path / "historic-sites" / METADATA_FILENAME).write_text(
            json.dumps({"labels": {"name": "名称"}}), encoding="utf-8"
        )

        existing = read_existing(tmp_path, datasets_for([MONUMENTS]))

        assert list(existing.records) == ["401/1"]
        assert existing.labels["historic-sites"] == {"name": "名称"}
        assert existing.files == 1

    def test_複合指定は2つのリポジトリに出ても1件(self, tmp_path: Path) -> None:
        """401 の複合指定は両方のリポジトリへ同じ行を書いてある (ADR 0012)。"""
        same = record(ledger_id="401", managed_id="712", types=["特別名勝", "特別史跡"])
        for repo in ("special-historic-sites", "special-places-of-scenic-beauty"):
            write_records(tmp_path, repo, "13_tokyo.jsonl", [same])

        existing = read_existing(tmp_path, datasets_for([MONUMENTS]))

        assert list(existing.records) == ["401/712"]
        assert existing.files == 2

    def test_出力が無ければ空(self, tmp_path: Path) -> None:
        assert read_existing(tmp_path, datasets_for([MONUMENTS])).records == {}

    def test_知らない分類の行は例外にする(self, tmp_path: Path) -> None:
        """台帳ID から分類を戻せないと、消えたときの扱いも書き先も決まらない。"""
        write_records(
            tmp_path, "historic-sites", "13_tokyo.jsonl", [record(ledger_id="901")]
        )

        with pytest.raises(UpdateError, match="知らない分類"):
            read_existing(tmp_path, datasets_for([MONUMENTS]))

    def test_読めない行は例外にする(self, tmp_path: Path) -> None:
        """黙って飛ばすと、その行が新規や指定解除に化ける。差分の基準が壊れたら進まない。"""
        path = write_records(tmp_path, "historic-sites", "13_tokyo.jsonl", [record()])
        path.write_text('{"ledger_id": "401"}\n', encoding="utf-8")

        with pytest.raises(UpdateError, match="1 行目"):
            read_existing(tmp_path, datasets_for([MONUMENTS]))


class Test計画:
    def existing(self) -> Existing:
        return Existing(
            records={
                "101/keep": record(managed_id="keep", name="そのまま"),
                "101/rotate": record(managed_id="rotate", name="巡回"),
                "101/changed": record(managed_id="changed", name="古い名前"),
                "101/gone": record(managed_id="gone", name="消えた"),
            }
        )

    def rows(self) -> list[LedgerRow]:
        return [
            row(REGISTERED, {"管理対象ID": "keep", "名称": "そのまま"}),
            row(REGISTERED, {"管理対象ID": "rotate", "名称": "巡回"}),
            row(REGISTERED, {"管理対象ID": "changed", "名称": "新しい名前"}),
            row(REGISTERED, {"管理対象ID": "added", "名称": "新規"}),
        ]

    def plan(self, **overrides: Any):  # type: ignore[no-untyped-def]
        settings: dict[str, Any] = {
            "slot": rotation_slot("101/rotate"),
            "complete_categories": ALL_CATEGORIES,
        }
        return plan_update(self.existing(), self.rows(), **(settings | overrides))

    def test_新規と変更と巡回だけを取り直す(self) -> None:
        plan = self.plan()

        assert {item.key: item.reason for item in plan.refetch} == {
            "101/added": Reason.ADDED,
            "101/changed": Reason.CHANGED,
            "101/rotate": Reason.ROTATED,
        }
        assert plan.unchanged == 1
        assert [target.name for target in plan.targets if target.key == "101/added"] == ["新規"]

    def test_変更は理由まで残す(self) -> None:
        changed = next(item for item in self.plan().refetch if item.reason is Reason.CHANGED)
        assert changed.changes == ("名称",)

    def test_台帳から消えた行は落とす(self) -> None:
        plan = self.plan()

        assert plan.removed == ["101/gone"]
        assert plan.retained == []

    def test_網羅性を確かめられない分類では消さない(self) -> None:
        """取りこぼしや相手側の一時的な障害を指定解除と読み違えないため (ADR 0018)。"""
        plan = self.plan(complete_categories=set())

        assert plan.removed == []
        assert plan.retained == ["101/gone"]

    def test_巡回の枠が違えば取り直さない(self) -> None:
        plan = self.plan(slot=(rotation_slot("101/rotate") + 1) % ROTATION_MONTHS)

        assert {item.reason for item in plan.refetch} == {Reason.ADDED, Reason.CHANGED}
        assert plan.unchanged == 2

    def test_同じ棟が複数の地域に出ても一度だけ(self) -> None:
        """102 の琵琶湖疏水施設は滋賀県と京都府の両方の CSV に出る (#6 の実測)。"""
        duplicated = [row(REGISTERED, {"管理対象ID": "keep", "名称": "そのまま"})] * 2

        plan = plan_update(
            self.existing(), duplicated, slot=99, complete_categories=set()
        )

        assert plan.unchanged == 1

    def test_手を入れるところが無ければ空(self) -> None:
        plan = plan_update(Existing(), [], slot=0, complete_categories=ALL_CATEGORIES)

        assert plan.is_empty
        assert not plan.refetch


class Test使い回すぶん:
    def test_落とす行だけを外す(self) -> None:
        existing = Test計画().existing()
        plan = plan_update(
            existing,
            Test計画().rows(),
            slot=rotation_slot("101/rotate"),
            complete_categories=ALL_CATEGORIES,
        )

        reuse = reuse_for(plan, existing, "2026-09-01T00:00:00+00:00")

        assert "101/gone" not in reuse.records
        assert set(reuse.records) == {"101/keep", "101/rotate", "101/changed"}
        assert reuse.retained == []
        assert reuse.accessed_at == "2026-09-01T00:00:00+00:00"

    def test_残す行は書き出しへ渡す(self) -> None:
        """網羅性を確かめられない分類の行は、台帳に出なくても出力に残す。"""
        existing = Test計画().existing()
        plan = plan_update(existing, Test計画().rows(), slot=0, complete_categories=set())

        reuse = reuse_for(plan, existing, "")

        assert [item["managed_id"] for item in reuse.retained] == ["gone"]
        assert "101/gone" in reuse.records

    def test_取り直す行も渡す(self) -> None:
        """取得に失敗しても前回の行が残るようにする — 失敗を行の消失にしない。"""
        existing = Test計画().existing()
        plan = plan_update(
            existing,
            Test計画().rows(),
            slot=rotation_slot("101/rotate"),
            complete_categories=ALL_CATEGORIES,
        )

        assert "101/changed" in reuse_for(plan, existing, "").records


def test_計画の報告に件数と理由が出る() -> None:
    existing = Test計画().existing()
    plan = plan_update(
        existing,
        Test計画().rows(),
        slot=rotation_slot("101/rotate"),
        complete_categories=ALL_CATEGORIES,
    )

    text = format_plan(plan, existing)

    assert "前回の出力 4 件" in text
    assert "取り直す 3 件 = 新規 1 / 台帳の値が変わった 1 / 巡回 1" in text
    assert "そのまま 1 件" in text
    assert "落とす (指定解除) 1 件" in text
    assert "101/changed 新しい名前 (名称)" in text
