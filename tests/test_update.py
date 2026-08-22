"""差分更新層のテスト (ADR 0018 / ADR 0020)。

**外部サイトへは出ない。** 前回の出力は ``tmp_path`` に書いた JSON Lines で、
台帳はキャッシュに置いた CSV。ここで守りたいのは 3 つ。

- 取り直すのは新規・台帳の値が変わったぶん・巡回の 1/52 だけ
- 巡回の割り当てが実行のたびに変わらない (変わると毎週ちがう 1/52 を取る)
- **取りこぼしを指定解除と読み違えて行を消さない**
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from conftest import make_csv, make_row
from heritage_crawler.catalog import DESIGNATED, MONUMENTS, REGISTERED, Category, datasets_for
from heritage_crawler.detail import Presence
from heritage_crawler.export import REMOVED_FILENAME
from heritage_crawler.ledger import LedgerRow
from heritage_crawler.metadata import METADATA_FILENAME
from heritage_crawler.update import (
    ROTATION_SLOTS,
    Existing,
    LedgerDiff,
    Reason,
    UpdateError,
    UpdatePlan,
    candidates,
    compare_ledgers,
    format_plan,
    plan_update,
    read_existing,
    reuse_for,
    rotation_slot,
    verify_removals,
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


def write_removed(output_dir: Path, repo: str, entries: list[dict[str, Any]]) -> Path:
    """データリポジトリのルートに置く削除の記録 (ADR 0021)。"""
    path = output_dir / repo / REMOVED_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in entries), encoding="utf-8"
    )
    return path


class Test巡回の割り当て:
    def test_実行のたびに同じ枠へ入る(self) -> None:
        """組み込みの hash は実行ごとに種が変わるので使えない (毎月ちがう 1/12 を取ってしまう)。"""
        assert rotation_slot("401/00003452") == rotation_slot("401/00003452") == 5

    def test_枠は0から51(self) -> None:
        slots = {rotation_slot(f"101/{number}") for number in range(2000)}
        assert slots == set(range(ROTATION_SLOTS))

    def test_キーが違えば枠も散る(self) -> None:
        """同じ台帳ID の連番が同じ枠に固まると、その週に負荷が寄る。"""
        slots = [rotation_slot(f"101/{number:08d}") for number in range(520)]
        assert max(slots.count(slot) for slot in set(slots)) < 30


class Test台帳同士の突き合わせ:
    """前回の台帳と今回の台帳を CSV 同士で比べる (ADR 0020)。

    前は CSV の値と出力 (JSON Lines) の値を比べていたので、意味が揃う列だけを選ぶ・
    分類ごとに読み替える・空欄は飛ばす、という近似が要った。同じ台帳どうしなら
    18 列を素直に比べられる。
    """

    def ledger(self, root: Path, name: str, rows: list[dict[str, str]]) -> Path:
        """台帳の CSV を 1 つ書く。`root` は cache/ledger に当たるディレクトリ。"""
        path = root / REGISTERED.code / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            make_csv([make_row({"台帳ID": REGISTERED.code, **values}) for values in rows])
        )
        return path

    def compare(self, tmp_path: Path, before: list[dict[str, str]], after: list[dict[str, str]]):  # type: ignore[no-untyped-def]
        self.ledger(tmp_path / "before", "13-tokyo.csv", before)
        self.ledger(tmp_path / "after", "13-tokyo.csv", after)
        return compare_ledgers(tmp_path / "before", tmp_path / "after", [REGISTERED])

    def test_バイトが同じファイルは変化として数えない(self, tmp_path: Path) -> None:
        rows = [{"管理対象ID": "1", "名称": "宮下家住宅主屋", "所在地": "横浜市"}]
        diff = self.compare(tmp_path, rows, rows)

        assert diff.changed_files == ()
        assert diff.changed == {}
        assert diff.compared_files == 1

    def test_値が変わった列を名指しする(self, tmp_path: Path) -> None:
        diff = self.compare(
            tmp_path,
            [{"管理対象ID": "1", "名称": "古い名前", "緯度": "35.4"}],
            [{"管理対象ID": "1", "名称": "新しい名前", "緯度": "35.5"}],
        )

        assert diff.changed == {"101/1": ("名称", "緯度")}
        assert diff.changed_files == ("101/13-tokyo.csv",)

    def test_空欄になった変更も見つける(self, tmp_path: Path) -> None:
        """CSV 同士なので「値が消えた」も差分になる (出力との比較では飛ばしていた)。"""
        diff = self.compare(
            tmp_path,
            [{"管理対象ID": "1", "所在地": "横浜市"}],
            [{"管理対象ID": "1", "所在地": ""}],
        )
        assert diff.changed == {"101/1": ("所在地",)}

    def test_並びが変わっただけなら差分にしない(self, tmp_path: Path) -> None:
        """相手先の並び順が安定している保証は無い。バイトは違っても中身は同じ。"""
        rows = [
            {"管理対象ID": "1", "名称": "一つ目"},
            {"管理対象ID": "2", "名称": "二つ目"},
        ]
        diff = self.compare(tmp_path, rows, list(reversed(rows)))

        assert diff.changed_files == ("101/13-tokyo.csv",)  # バイトは違う
        assert diff.changed == {}  # 中身は同じ

    def test_前回に無い行はここでは扱わない(self, tmp_path: Path) -> None:
        """追加は台帳と出力の突き合わせが受け持つ (前回の台帳が無い回でも働くため)。"""
        diff = self.compare(
            tmp_path,
            [{"管理対象ID": "1", "名称": "もとから"}],
            [{"管理対象ID": "1", "名称": "もとから"}, {"管理対象ID": "2", "名称": "新規"}],
        )
        assert diff.changed == {}

    def test_地域をまたいで移った行は変更になる(self, tmp_path: Path) -> None:
        """片方の CSV で消えてもう片方に現れる。**両方のファイルが変化するので拾える。**"""
        moved = {"管理対象ID": "1", "名称": "移った指定"}
        self.ledger(tmp_path / "before", "13-tokyo.csv", [{**moved, "都道府県": "東京都"}])
        self.ledger(tmp_path / "before", "26-kyoto.csv", [])
        self.ledger(tmp_path / "after", "13-tokyo.csv", [])
        self.ledger(tmp_path / "after", "26-kyoto.csv", [{**moved, "都道府県": "京都府"}])

        diff = compare_ledgers(tmp_path / "before", tmp_path / "after", [REGISTERED])
        assert diff.changed == {"101/1": ("都道府県",)}

    def test_前回の台帳が無ければ全ファイルが変化(self, tmp_path: Path) -> None:
        """初回。突き合わせる相手がいないので、行の変更は 1 件も出ない。"""
        self.ledger(tmp_path / "after", "13-tokyo.csv", [{"管理対象ID": "1", "名称": "初回"}])

        diff = compare_ledgers(tmp_path / "before", tmp_path / "after", [REGISTERED])
        assert diff.changed_files == ("101/13-tokyo.csv",)
        assert diff.changed == {}


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

    def test_どのリポジトリに居たかを覚える(self, tmp_path: Path) -> None:
        """落とした行をどの `removed.jsonl` へ書くかは、前回の居場所で決まる (ADR 0021)。

        `records` はキーで畳むので、複合指定がどちらのリポジトリに居たかが消える。
        """
        same = record(ledger_id="401", managed_id="712", types=["特別名勝", "特別史跡"])
        for repo in ("special-historic-sites", "special-places-of-scenic-beauty"):
            write_records(tmp_path, repo, "13_tokyo.jsonl", [same])
        write_records(tmp_path, "historic-sites", "13_tokyo.jsonl", [record(ledger_id="401")])

        existing = read_existing(tmp_path, datasets_for([MONUMENTS]))

        assert existing.repos["401/712"] == {
            "special-historic-sites",
            "special-places-of-scenic-beauty",
        }
        assert existing.repos["401/1"] == {"historic-sites"}

    def test_前回の削除の記録も読む(self, tmp_path: Path) -> None:
        """`missing_since` を据え置くには、前回の記録が要る (毎回今日にすると揺れる)。"""
        write_records(tmp_path, "historic-sites", "13_tokyo.jsonl", [record(ledger_id="401")])
        write_removed(
            tmp_path,
            "historic-sites",
            [{"ledger_id": "401", "managed_id": "9", "missing_since": "2026-08-03"}],
        )

        existing = read_existing(tmp_path, datasets_for([MONUMENTS]))

        assert existing.removed["historic-sites"]["401/9"]["missing_since"] == "2026-08-03"

    def test_出力が無ければ空(self, tmp_path: Path) -> None:
        assert read_existing(tmp_path, datasets_for([MONUMENTS])).records == {}

    def test_知らない分類の行は例外にする(self, tmp_path: Path) -> None:
        """分類を戻せないと、消えたときの扱いも書き先も決まらない。"""
        write_records(
            tmp_path, "historic-sites", "13_tokyo.jsonl", [record(ledger_id="999")]
        )

        with pytest.raises(UpdateError, match="分類を戻せない"):
            read_existing(tmp_path, datasets_for([MONUMENTS]))

    def test_分類は台帳ID_ではなく分類コードから戻す(self, tmp_path: Path) -> None:
        """台帳ID 401 には 401 / 411 / 412 が同居する (#74 / ADR 0024)。

        台帳ID を見ていたら、知らない分類コードを持つ行を素通ししてしまう。
        """
        write_records(
            tmp_path,
            "historic-sites",
            "13_tokyo.jsonl",
            [record(ledger_id="401") | {"category_code": "999"}],
        )

        with pytest.raises(UpdateError, match="分類を戻せない"):
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
            # 台帳同士の突き合わせ (ADR 0020) が見つけたぶん。
            "diff": LedgerDiff(changed={"101/changed": ("名称",)}),
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
        plan = self.plan(slot=(rotation_slot("101/rotate") + 1) % ROTATION_SLOTS)

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


class Test存在確認で落とすかを決め直す:
    """台帳の不在は消極的な証拠。詳細ページの返答を足して結論を決める (ADR 0021)。

    | 網羅性 | 存在確認 | 落とすか | 結論 |
    |---|---|---|---|
    | ○ | gone | 落とす | `delisted` |
    | ○ | alive | 落とす | `unlisted` |
    | ○ | unknown | **落とさない** | — |
    | × | gone | 落とす | `delisted` |
    | × | alive / unknown | 落とさない | — |
    """

    def plan(self, presence: Presence | None, *, complete: bool = True):  # type: ignore[no-untyped-def]
        existing = Test計画().existing()
        plan = plan_update(
            existing,
            Test計画().rows(),
            slot=0,
            complete_categories=ALL_CATEGORIES if complete else set(),
        )
        # presence が None = 存在確認そのものを走らせない経路 (--dry-run など)
        if presence is not None:
            verify_removals(plan, {"101/gone": presence})
        return plan, existing

    def conclusion(self, plan: Any, existing: Existing) -> str | None:
        removals = reuse_for(plan, existing, "2026-08-17T00:00:00+00:00").removals
        return removals["101/gone"]["conclusion"] if removals else None

    def test_無いと答えたら落として指定解除とする(self) -> None:
        plan, existing = self.plan(Presence.GONE)

        assert plan.removed == ["101/gone"]
        assert self.conclusion(plan, existing) == "delisted"

    def test_詳細が生きていれば落とすが結論は変える(self) -> None:
        """台帳から外れただけで、データベースにはまだ居る状態。"""
        plan, existing = self.plan(Presence.ALIVE)

        assert plan.removed == ["101/gone"]
        assert self.conclusion(plan, existing) == "unlisted"

    def test_確かめられなければ落とさない(self) -> None:
        """混んでいる時間帯に当たった 1 件を指定解除にしないため。翌週やり直す。"""
        plan, _ = self.plan(Presence.UNKNOWN)

        assert plan.removed == []
        assert plan.retained == ["101/gone"]

    def test_網羅性を確かめられなくても無いと答えたら落とす(self) -> None:
        """102 は全国件数とキーの異なり数を直接比べられない。ここが唯一の証拠になる。"""
        plan, existing = self.plan(Presence.GONE, complete=False)

        assert plan.removed == ["101/gone"]
        assert self.conclusion(plan, existing) == "delisted"
        assert existing  # 使う

    def test_網羅性も詳細も確かめられなければ残す(self) -> None:
        plan, _ = self.plan(Presence.ALIVE, complete=False)

        assert plan.removed == []
        assert plan.retained == ["101/gone"]

    def test_証拠に確認の結果が残る(self) -> None:
        plan, existing = self.plan(Presence.GONE)

        evidence = reuse_for(plan, existing, "2026-08-17T00:00:00+00:00").removals["101/gone"][
            "evidence"
        ]
        assert evidence["detail_page"] == "gone"
        assert evidence["category_complete"] is True

    def test_確認しなければ結論は保留のまま(self) -> None:
        """`--dry-run` など、通信しない経路では今までどおり落とす。"""
        plan, existing = self.plan(None)

        assert plan.removed == ["101/gone"]
        assert self.conclusion(plan, existing) == "unverified"


class Test格上げの見分け:
    """101 が 102 に指定されると登録は抹消され、台帳ID が変わって別レコードになる。

    手元からは「101 から 1 件消えて 102 に 1 件現れた」と見える。削除ではなく
    価値が公認された出来事なので、「解除された」と記録してはいけない (ADR 0021)。
    """

    def existing(self) -> Existing:
        return Existing(
            records={
                "101/gone": record(
                    managed_id="gone",
                    name="旧亀岡家住宅",
                    latitude=36.1,
                    longitude=140.2,
                    address="茨城県",
                )
            }
        )

    def plan(self, added: Mapping[str, str]):  # type: ignore[no-untyped-def]
        rows = [row(DESIGNATED, {"管理対象ID": "new", **added})]
        return plan_update(
            self.existing(), rows, slot=0, complete_categories=ALL_CATEGORIES
        )

    def test_名称と緯度経度が一致する新規があれば格上げとみなす(self) -> None:
        plan = self.plan({"名称": "旧亀岡家住宅", "緯度": "36.1", "経度": "140.2"})
        verify_removals(plan, {"101/gone": Presence.GONE})

        removed = reuse_for(plan, self.existing(), "2026-08-17T00:00:00+00:00").removals

        assert removed["101/gone"]["conclusion"] == "reclassified"
        assert removed["101/gone"]["evidence"]["matched_elsewhere"] == ["102/new"]

    def test_名称が同じでも場所が違えば別物(self) -> None:
        """「本堂」のような名称は何十とある。名称だけでは同じものと言えない。"""
        plan = self.plan({"名称": "旧亀岡家住宅", "緯度": "35.0", "経度": "139.0"})
        verify_removals(plan, {"101/gone": Presence.GONE})

        removed = reuse_for(plan, self.existing(), "2026-08-17T00:00:00+00:00").removals

        assert removed["101/gone"]["conclusion"] == "delisted"


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

    def test_落とした行を証拠つきで渡す(self) -> None:
        """出力から落とすだけでは記録が残らない (ADR 0021)。

        判定を 1 語に潰さず、観測した証拠を並べる。PR の段階では詳細ページを
        確かめていないので `detail_page` は `unknown`、結論は `unverified`。
        """
        existing = Existing(
            records=Test計画().existing().records,
            repos={"101/gone": {"registered-tangible-cultural-properties"}},
            accessed_dates={"registered-tangible-cultural-properties": "2026-08-10"},
        )
        plan = plan_update(
            existing, Test計画().rows(), slot=0, complete_categories=ALL_CATEGORIES
        )

        reuse = reuse_for(plan, existing, "2026-08-17T00:00:00+00:00")

        assert list(reuse.removals) == ["101/gone"]
        removed = reuse.removals["101/gone"]
        assert removed["managed_id"] == "gone"
        assert removed["name"] == "消えた"
        assert removed["last_seen_date"] == "2026-08-10"
        assert removed["missing_since"] == "2026-08-17"
        assert removed["conclusion"] == "unverified"
        assert removed["evidence"] == {
            "absent_from_ledger": True,
            "category_complete": True,
            "detail_page": "unknown",
            "matched_elsewhere": None,
        }

    def test_残した行は削除の記録に入れない(self) -> None:
        """`retained` は「消したかどうかを断じられない」一時的な状態 (ADR 0021)。"""
        existing = Test計画().existing()
        plan = plan_update(existing, Test計画().rows(), slot=0, complete_categories=set())

        assert reuse_for(plan, existing, "2026-08-17T00:00:00+00:00").removals == {}

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
        diff=LedgerDiff(changed={"101/changed": ("名称",)}),
    )

    text = format_plan(plan, existing)

    assert "前回の出力 4 件" in text
    assert "取り直す 3 件 = 新規 1 / 台帳の値が変わった 1 / 巡回 1" in text
    assert "そのまま 1 件" in text
    assert "落とす (指定解除) 1 件" in text
    assert "101/changed 新しい名前 (名称)" in text


def test_存在を確かめる先は台帳ID_ではなく分類コードで組む() -> None:
    """詳細ページは分類コードで引く (#74 / ADR 0024)。

    台帳ID 401 の行が登録記念物 (411) なら、401 で引いてもエラーページしか
    返らない。指定解除でないものを解除と読み違える。
    """
    plan = UpdatePlan(slot=0, complete=frozenset())
    plan.removed.append("401/00003483")
    existing = Existing(
        records={
            "401/00003483": record(
                ledger_id="401", managed_id="00003483", category_code="103", name="函館公園"
            )
        }
    )

    found = candidates(plan, existing)

    assert [target.url for target in found] == [
        "https://kunishitei.bunka.go.jp/heritage/detail/103/00003483"
    ]
