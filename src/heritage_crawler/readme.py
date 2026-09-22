"""README の表を、散文ではなく正本そのものから組み立てる (Issue #37 / Issue #102)。

差し込み口は 2 つあり、**正本も検査できる場所も違う**。

| 表 | 正本 | 検査できる場所 |
|---|---|---|
| 件数表 | 書き出したデータ (``meta.json`` と JSON Lines) | 週次のみ (CI に ``data`` が無い) |
| 出力先リポジトリ | ``catalog.TARGET_DATASETS`` | テスト = PR の CI |

散文に手で書いた件数は、新規指定・解除のたびに静かに嘘になる。**正本は既にある**
— 各データリポジトリの ``meta.json`` (ADR 0014) と JSON Lines そのもの。README の
表はそこから生成し、``heritage-crawler render-readme`` で作り直す。

数え方を 2 通り出すのは、**行数の合計が取得対象にならない**ため。401 には種別を
2 つ持つ複合指定があり、両方のリポジトリへ同じ行を書く (ADR 0012)。突き合わせに
使えるのは ``(台帳ID, 管理対象ID)`` の異なり数だけで、これは 1 つの ``meta.json``
からは出せない (リポジトリをまたぐ重複はそのリポジトリからは見えない)。

**利用日はここで扱わない。** 出典表記が求める「◯年◯月◯日に利用」はデータセット
ごとに違い、月次更新では実行日になる (ADR 0018)。README へ写せば毎月ドリフトする
ので、正本は ``meta.json`` の ``source.accessed_date`` に置いたままにして、README
には日付を含まない表記例だけを載せる。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from heritage_crawler.catalog import TARGET_CATEGORIES, TARGET_DATASETS, Category
from heritage_crawler.metadata import metadata_path
from heritage_crawler.record import DESIGNATION_KINDS

BEGIN_MARKER: Final = "<!-- generated: heritage-crawler render-readme -->"
END_MARKER: Final = "<!-- /generated -->"
"""差し込み口。**生成物であることを読む人にも見せる**ための目印でもある。"""

DATASETS_BEGIN_MARKER: Final = "<!-- generated: heritage-crawler render-readme (datasets) -->"
"""出力先リポジトリの表の差し込み口 (Issue #102)。

**件数表と違って、この表はデータを読まずに作れる** — 中身は ``TARGET_DATASETS``
だけで決まる。だからテストが README と直接突き合わせられ、分類を足した PR の CI が
その場で赤くなる (``tests/test_readme.py``)。件数表の方は書き出したデータを読むので、
``data`` を持たない CI では確かめようがなく、週次に頼るしかない。

手書きの表を置いていた頃は、分類が 4 から 19 へ増えたときに 10 行のまま取り残された
(Issue #100)。地の文に写した数と同じで、**誰も検査していないものは静かに嘘になる**。
"""

HEADERS: Final[tuple[str, ...]] = (
    "分類コード",
    "文化財種別",
    "指定行為",
    "指定件数",
    "取得対象",
    "収録行数",
)

DATASETS_HEADERS: Final[tuple[str, ...]] = ("リポジトリ", "取得対象")


class ReadmeError(RuntimeError):
    """データを読めない、または README に差し込み口が無い。"""


@dataclass(frozen=True)
class CategoryCounts:
    """1 分類ぶんの数え上げ。"""

    category: Category

    records: int
    """``(台帳ID, 管理対象ID)`` の異なり数 = 取得対象。全国件数と比べられる単位。"""

    rows: int
    """書き出した行数。複合指定を持つ 401 では ``records`` を上回る (ADR 0012)。"""


def read_counts(
    output_dir: Path, categories: Sequence[Category] = TARGET_CATEGORIES
) -> list[CategoryCounts]:
    """データリポジトリを走査して、分類ごとの件数を数える。

    ``meta.json`` の件数と JSON Lines の行数が食い違ったら進まない。片方だけ
    古い状態から README を作ると、**生成物なのに実態と合わない**という一番たちの
    悪いものができるため。
    """
    wanted = {category.code for category in categories}
    keys: dict[str, set[str]] = {code: set() for code in wanted}
    rows: dict[str, int] = dict.fromkeys(wanted, 0)

    for dataset in TARGET_DATASETS:
        code = dataset.category.code
        if code not in wanted:
            continue
        declared = _declared_records(metadata_path(output_dir, dataset))
        found = list(_read_keys(output_dir / dataset.repo / "data"))
        if len(found) != declared:
            raise ReadmeError(
                f"{dataset.repo}: meta.json の件数 {declared:,} と JSON Lines の行数 "
                f"{len(found):,} が食い違う。build-records で書き直してから作り直す"
            )
        rows[code] += len(found)
        keys[code].update(found)

    return [
        CategoryCounts(category, len(keys[category.code]), rows[category.code])
        for category in categories
    ]


def render_block(counts: Sequence[CategoryCounts]) -> str:
    """件数表を差し込み口ごと組み立てる。並びは ``TARGET_CATEGORIES`` のまま。"""
    lines = [
        BEGIN_MARKER,
        "| " + " | ".join(HEADERS) + " |",
        "|" + "---|" * len(HEADERS),
    ]
    for item in counts:
        lines.append(
            f"| {item.category.code} | {item.category.name} "
            f"| {DESIGNATION_KINDS[item.category.code]} "
            f"| {item.category.known_designation_count:,} "
            f"| {item.records:,} | {item.rows:,} |"
        )
    designations = sum(item.category.known_designation_count for item in counts)
    lines.append(
        f"| **計** | | | **{designations:,}** "
        f"| **{sum(item.records for item in counts):,}** "
        f"| **{sum(item.rows for item in counts):,}** |"
    )
    lines.append(END_MARKER)
    return "\n".join(lines)


def render_datasets_block() -> str:
    """出力先リポジトリの一覧を ``TARGET_DATASETS`` から組み立てる。

    **データを読まない。** 分類とリポジトリの対応は ``catalog`` だけで決まるので、
    書き出したデータが手元に無い環境 (CI) でも作れて、突き合わせられる。

    >>> render_datasets_block().splitlines()[3]
    '| `registered-tangible-cultural-properties` | 101 登録有形文化財（建造物） |'
    """
    lines = [
        DATASETS_BEGIN_MARKER,
        "| " + " | ".join(DATASETS_HEADERS) + " |",
        "|" + "---|" * len(DATASETS_HEADERS),
    ]
    lines.extend(
        f"| `{dataset.repo}` | {dataset.category.code} {dataset.name} |"
        for dataset in TARGET_DATASETS
    )
    lines.append(END_MARKER)
    return "\n".join(lines)


def replace_block(text: str, block: str, begin: str = BEGIN_MARKER) -> str:
    """README の差し込み口の中身を入れ替える。外側は 1 文字も動かさない。

    閉じは差し込み口で共通 (``END_MARKER``)。開きだけで場所が決まるので、
    ``begin`` に渡したものが**どちらの表か**を決める。
    """
    before, opened, rest = text.partition(begin)
    if not opened:
        raise ReadmeError(f"差し込み口 {begin} が無い")
    _, closed, after = rest.partition(END_MARKER)
    if not closed:
        raise ReadmeError(f"差し込み口の閉じ {END_MARKER} が無い")
    if begin in after:
        raise ReadmeError(f"差し込み口 {begin} が 2 か所以上ある。どこを書き換えるか決まらない")
    return before + block + after


def _declared_records(path: Path) -> int:
    """``meta.json`` が申告している収録件数。

    **無いリポジトリは飛ばさない。** 表から 1 分類が黙って抜けるより、読めないと
    言って止まる方がよい (README は全分類を載せる前提で書かれている)。
    """
    if not path.is_file():
        raise ReadmeError(f"{path} が無い。--output-dir がデータリポジトリの親か確かめる")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return int(payload["counts"]["records"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ReadmeError(f"{path} を読めない: {error}") from error


def _read_keys(directory: Path) -> Iterator[str]:
    """JSON Lines から ``(台帳ID, 管理対象ID)`` を 1 行ずつ取り出す。

    レコード全体は持たない。数えるのに要るのはキーだけで、2 万件ぶんの解説文を
    抱える理由が無い。
    """
    for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                yield f"{record['ledger_id']}/{record['managed_id']}"
            except (json.JSONDecodeError, KeyError, TypeError) as error:
                raise ReadmeError(f"{path} の {number} 行目を読めない: {error}") from error
