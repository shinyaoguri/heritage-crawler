"""データセットのメタデータ — 中身を機械的に読むための 1 枚 (ADR 0014)。

データリポジトリのルートに ``meta.json`` を置く。載せるのは、JSON Lines を
見ただけでは分からない 3 種類。

- **出典表記と利用日** — 規約が求める表示 (ADR 0007)。利用日は詳細ページを
  取得した日で、**散文に手で書くと月次更新で静かに嘘になる**唯一の値
- **表示名** — 原文ラベルは分類ごとに違い、同じキーへ複数の呼び名が寄っている。
  対応表を持つのはクローラーだけなので、実測した表示名をここから配る
- **語彙と件数** — 種別・時代などの取りうる値。読む側が全件を走査せずに
  絞り込みの選択肢を作れる

これがあると、データを読む側は分類ごとの差異を知らずに済む。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from importlib import metadata
from pathlib import Path
from typing import Any, Final

from heritage_crawler.cache import atomic_write
from heritage_crawler.catalog import BASE_URL, Area, Dataset
from heritage_crawler.record import KEY_ORDER

SCHEMA_VERSION: Final = 1
"""``meta.json`` の形式。読む側が非互換に気付けるように持たせる。

上げるときは ADR に記録する (読む側が黙って壊れるのを防ぐため)。
"""

METADATA_FILENAME: Final = "meta.json"

SOURCE_NAME: Final = "国指定文化財等データベース"
SOURCE_PUBLISHER: Final = "文化庁"
SOURCE_URL: Final = f"{BASE_URL}/"
SOURCE_TERMS_URL: Final = f"{BASE_URL}/top/policy"

JST: Final = timezone(timedelta(hours=9), "JST")
"""利用日を切るタイムゾーン。

``fetched_at`` は UTC で記録されるが、日本語の出典表記に載せる「◯年◯月◯日に
利用」は日本時間の日付を指す。UTC のまま日付を取ると、日本の夕方以降に取得した
ぶんが 1 日前にずれる。
"""

FACET_KEYS: Final[tuple[str, ...]] = (
    "types",
    "period",
    "prefecture",
    "criteria",
    "owner_type",
    "designation_kind",
    "national_treasure_class",
    "special_class",
)
"""値の分布を数えるキー。取りうる値が少なく、絞り込みの軸になるものだけ。

名称や解説文のような値がばらけるキーを入れると、``meta.json`` がデータの
写しになってしまう。
"""

COORDINATE_KEYS: Final = ("latitude", "longitude")


def accessed_date(fetched_at: str) -> str:
    """取得日時を、日本時間の日付にする。

    利用日は**詳細ページを取得した日**であって、組み立てを走らせた日ではない。
    ``build-records`` はキャッシュだけを読むので (ADR 0006)、3 か月前の
    キャッシュから組み立て直せば実行日は実態と合わなくなる。

    UTC の 15 時は日本時間の翌日 0 時にあたる。

    >>> accessed_date("2026-08-11T15:29:02+00:00")
    '2026-08-12'
    >>> accessed_date("2026-08-11T14:59:59+00:00")
    '2026-08-11'
    >>> accessed_date("")
    ''
    """
    if not fetched_at:
        return ""
    return datetime.fromisoformat(fetched_at).astimezone(JST).strftime("%Y-%m-%d")


def attribution(date: str) -> str:
    """出典表記を組み立てる (ADR 0007)。

    文部科学省ウェブサイト利用規約の記載例に合わせた 3 行。**「上記を加工して
    作成」まで含めて 1 つの表示**なので、切り離して使わない。

    >>> print(attribution("2026-08-12"))
    出典：「国指定文化財等データベース」（文化庁）
    （https://kunishitei.bunka.go.jp/）（2026年8月12日に利用）
    上記を加工して作成
    """
    year, month, day = (int(part) for part in date.split("-"))
    return (
        f"出典：「{SOURCE_NAME}」（{SOURCE_PUBLISHER}）\n"
        f"（{SOURCE_URL}）（{year}年{month}月{day}日に利用）\n"
        "上記を加工して作成"
    )


def generator_version() -> str:
    """インストール済みのクローラーの版。どの版が書いたか追えるようにする。"""
    try:
        return metadata.version("heritage-crawler")
    except metadata.PackageNotFoundError:  # pragma: no cover - 常にインストールして使う
        return "unknown"


def metadata_path(output_dir: Path, dataset: Dataset) -> Path:
    """``meta.json`` はデータリポジトリのルートに置く。

    規約上の義務はデータに付いているので、来歴もデータと一緒に旅する必要がある。
    中央に 1 枚だけ置くと、リポジトリを 1 つだけ複製した人が正しい出典表記を
    作れなくなる (ADR 0014)。
    """
    return output_dir / dataset.repo / METADATA_FILENAME


def build_metadata(
    dataset: Dataset,
    groups: Sequence[tuple[Area, Sequence[Mapping[str, Any]]]],
    labels: Mapping[str, str],
    fetched_at: str,
    version: str,
    accessed: str = "",
) -> dict[str, Any]:
    """1 データセットぶんの ``meta.json`` を組み立てる。

    ``groups`` は書き出したファイルと同じ単位 (地域ごとのレコード列) で、
    並びは出力順のまま渡す。数え直すのはここだけにして、出力と件数がずれる
    余地を作らない。``fetched_at`` はそのデータセットで最も新しい取得日時。

    ``accessed`` を渡すとその日付をそのまま使う。**行が 1 つも変わらなかった回に
    前回の利用日を据え置く**ための口で (ADR 0020)、据え置けば ``meta.json`` も
    1 バイトも変わらず、意味のないコミットが立たない。
    """
    date = accessed or accessed_date(fetched_at)
    fields: Counter[str] = Counter()
    facets: dict[str, Counter[str]] = {key: Counter() for key in FACET_KEYS}
    files: list[dict[str, Any]] = []
    with_coordinates = 0

    for area, records in groups:
        for record in records:
            fields.update(record.keys())
            if all(key in record for key in COORDINATE_KEYS):
                with_coordinates += 1
            for key, counter in facets.items():
                counter.update(_facet_values(record.get(key)))
        files.append(
            {
                "path": f"data/{area.code}_{area.slug}.jsonl",
                "area_code": area.code,
                "area_name": area.name,
                "records": len(records),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": {
            "repo": dataset.repo,
            "name": dataset.name,
            "category": {"code": dataset.category.code, "name": dataset.category.name},
            "kinds": list(dataset.kinds),
        },
        "source": {
            "name": SOURCE_NAME,
            "publisher": SOURCE_PUBLISHER,
            "url": SOURCE_URL,
            "terms_url": SOURCE_TERMS_URL,
            "accessed_date": date,
            "attribution": attribution(date) if date else "",
        },
        "generator": {"name": "heritage-crawler", "version": version},
        "counts": {
            "records": sum(len(records) for _, records in groups),
            "files": len(files),
            "with_coordinates": with_coordinates,
        },
        "labels": _ordered_labels(labels),
        "fields": {key: fields[key] for key in KEY_ORDER if key in fields},
        "facets": {key: _ranked(counter) for key, counter in facets.items() if counter},
        "files": files,
    }


def write_metadata(path: Path, payload: Mapping[str, Any]) -> None:
    """``meta.json`` を書く。整形して置くのは、人も読む 1 枚だから。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_write(path, text.encode("utf-8"))


def _facet_values(value: Any) -> list[str]:
    """1 レコードぶんの値を、数えられる文字列の列にする。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _ranked(counter: Counter[str]) -> dict[str, int]:
    """件数の多い順、同数なら値の順。並びを固定しないと差分が毎回出る。"""
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def _ordered_labels(labels: Mapping[str, str]) -> dict[str, str]:
    """表示名を ``KEY_ORDER`` の並びにする。附指定・措置の中のキーは親の直後。"""
    ordered: dict[str, str] = {}
    for key in KEY_ORDER:
        if key in labels:
            ordered[key] = labels[key]
        prefix = f"{key}."
        for nested in sorted(name for name in labels if name.startswith(prefix)):
            ordered[nested] = labels[nested]
    return ordered
