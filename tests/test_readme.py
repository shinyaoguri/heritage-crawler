"""README の件数表を生成するところのテスト。

**外部へは出ない。** 偽のデータリポジトリを ``tmp_path`` に組み立てて走査させる。

ここで守りたいのは 3 つ。

- **行数の合計を取得対象と取り違えない** — 401 の複合指定は 2 つのリポジトリに
  同じ行が出る (ADR 0012)
- **半端な状態から生成しない** — ``meta.json`` と JSON Lines が食い違ったり
  リポジトリが欠けていたら、表を作らずに止まる
- **差し込み口の外側を動かさない** — README の本文は人が書いたもの
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from heritage_crawler.catalog import MONUMENTS, REGISTERED, TARGET_CATEGORIES, TARGET_DATASETS
from heritage_crawler.readme import (
    BEGIN_MARKER,
    END_MARKER,
    CategoryCounts,
    ReadmeError,
    read_counts,
    render_block,
    replace_block,
)

HISTORIC = "historic-sites"
SCENIC = "special-places-of-scenic-beauty"


def put_dataset(output_dir: Path, repo: str, keys: list[tuple[str, str]], records: int) -> None:
    """1 データリポジトリぶんを書く。``records`` は ``meta.json`` の申告件数。

    申告と行数を別々に渡せるようにしてあるのは、食い違いを試すため。
    """
    directory = output_dir / repo / "data"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"ledger_id": ledger_id, "managed_id": managed_id}, ensure_ascii=False)
        for ledger_id, managed_id in keys
    ]
    (directory / "13_tokyo.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output_dir / repo / "meta.json").write_text(
        json.dumps({"counts": {"records": records}}), encoding="utf-8"
    )


def put_all(output_dir: Path, extra: dict[str, list[tuple[str, str]]] | None = None) -> None:
    """全 10 リポジトリを埋める。``extra`` を渡したリポジトリだけ行を持つ。"""
    rows = extra or {}
    for dataset in TARGET_DATASETS:
        keys = rows.get(dataset.repo, [])
        put_dataset(output_dir, dataset.repo, keys, len(keys))


def test_複合指定は行数では_2_異なりでは_1(tmp_path: Path) -> None:
    """401 の複合指定は両方のリポジトリへ同じ行を書く (ADR 0012)。

    行数を足し上げると取得対象を上回る。全国件数と比べられるのは異なりの側だけ。
    """
    put_all(tmp_path, {HISTORIC: [("401", "00003452")], SCENIC: [("401", "00003452")]})

    counts = {item.category.code: item for item in read_counts(tmp_path)}
    assert counts["401"].records == 1
    assert counts["401"].rows == 2


def test_分類ごとに数える(tmp_path: Path) -> None:
    put_all(
        tmp_path,
        {
            "registered-tangible-cultural-properties": [("101", "1"), ("101", "2")],
            HISTORIC: [("401", "3")],
        },
    )

    counts = {item.category.code: item.records for item in read_counts(tmp_path)}
    assert counts["101"] == 2
    assert counts["401"] == 1
    # 書いていない分類は 0。全 19 分類が並ぶ (ADR 0025)。
    assert set(counts) == {category.code for category in TARGET_CATEGORIES}
    assert sum(counts.values()) == 3


def test_分類を絞れる(tmp_path: Path) -> None:
    put_all(tmp_path, {HISTORIC: [("401", "3")]})

    assert [item.category for item in read_counts(tmp_path, [MONUMENTS])] == [MONUMENTS]


def test_申告と行数が食い違ったら止まる(tmp_path: Path) -> None:
    """片方だけ古い状態から作ると、生成物なのに実態と合わないものができる。"""
    put_all(tmp_path)
    put_dataset(tmp_path, HISTORIC, [("401", "3")], records=2)

    with pytest.raises(ReadmeError, match="食い違う"):
        read_counts(tmp_path)


def test_meta_json_が無いリポジトリがあったら止まる(tmp_path: Path) -> None:
    """表から 1 分類が黙って抜けるより、読めないと言って止まる方がよい。"""
    put_all(tmp_path)
    (tmp_path / HISTORIC / "meta.json").unlink()

    with pytest.raises(ReadmeError, match=r"meta\.json が無い"):
        read_counts(tmp_path)


def test_読めない行は止まる(tmp_path: Path) -> None:
    put_all(tmp_path)
    (tmp_path / HISTORIC / "data" / "13_tokyo.jsonl").write_text("{壊れた\n", encoding="utf-8")

    with pytest.raises(ReadmeError, match="1 行目"):
        read_counts(tmp_path)


def test_キーの無い行は止まる(tmp_path: Path) -> None:
    """``(台帳ID, 管理対象ID)`` が無ければ数えようがない。"""
    put_all(tmp_path)
    path = tmp_path / HISTORIC / "data" / "13_tokyo.jsonl"
    path.write_text('{"name": "跡"}\n', encoding="utf-8")

    with pytest.raises(ReadmeError, match="1 行目"):
        read_counts(tmp_path)


def test_空行は数えない(tmp_path: Path) -> None:
    """行数は申告と突き合わせるので、空行を数えると食い違いに化ける。"""
    put_all(tmp_path, {HISTORIC: [("401", "3")]})
    directory = tmp_path / HISTORIC / "data"
    (directory / "13_tokyo.jsonl").write_text(
        '{"ledger_id": "401", "managed_id": "3"}\n\n', encoding="utf-8"
    )

    counts = {item.category.code: item.rows for item in read_counts(tmp_path)}
    assert counts["401"] == 1


def test_表は分類の定義順で計を持つ() -> None:
    counts = [
        CategoryCounts(REGISTERED, records=14748, rows=14748),
        CategoryCounts(MONUMENTS, records=3281, rows=3395),
    ]

    block = render_block(counts)

    assert block.startswith(BEGIN_MARKER)
    assert block.endswith(END_MARKER)
    body = block.splitlines()
    assert body[3] == "| 101 | 登録有形文化財（建造物） | 登録 | 14,748 | 14,748 | 14,748 |"
    assert body[4] == "| 401 | 史跡名勝天然記念物 | 指定 | 3,281 | 3,281 | 3,395 |"
    assert body[5] == "| **計** | | | **18,029** | **18,029** | **18,143** |"


def test_差し込み口の外側は動かない() -> None:
    text = f"前書き\n\n{BEGIN_MARKER}\n古い表\n{END_MARKER}\n\n後書き\n"

    assert replace_block(text, f"{BEGIN_MARKER}\n新しい表\n{END_MARKER}") == (
        f"前書き\n\n{BEGIN_MARKER}\n新しい表\n{END_MARKER}\n\n後書き\n"
    )


@pytest.mark.parametrize(
    "text",
    [
        "差し込み口が無い",
        f"{BEGIN_MARKER}\n閉じが無い",
        f"{BEGIN_MARKER}\n{END_MARKER}\n{BEGIN_MARKER}\n{END_MARKER}",
    ],
    ids=["開きが無い", "閉じが無い", "2 か所ある"],
)
def test_差し込み口が定まらない_README_は拒む(text: str) -> None:
    with pytest.raises(ReadmeError):
        replace_block(text, f"{BEGIN_MARKER}\n{END_MARKER}")


def test_対応表に無いリポジトリは走査しない(tmp_path: Path) -> None:
    """出力先の正本は ``catalog.TARGET_DATASETS`` (ADR 0009)。

    親ディレクトリに別のリポジトリが並んでいても数に混ぜない。
    """
    put_all(tmp_path, {HISTORIC: [("401", "3")]})
    put_dataset(tmp_path, "heritages", [("401", "9")], records=1)

    counts = {item.category.code: item.rows for item in read_counts(tmp_path)}
    assert counts["401"] == 1
