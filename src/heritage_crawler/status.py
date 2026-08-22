"""確認した日の記録 — 静かなのか止まっているのかを分ける 1 枚 (ADR 0023)。

データリポジトリのルートに ``status.json`` を置く。**中身が動かない週も毎週書く**
のがこのファイルの全部で、``meta.json`` (ADR 0014) とは向いている先が違う。

- ``meta.json`` の**利用日** … そのデータを**取り出した**日。上流が変わらなければ
  古いままで、それが正常
- ``status.json`` の**確認日** … データベースを**見にいった**日。毎週動き、
  週次が止まればその日で固まる

2 つが並んで初めて「先週も確かめた。中身が 8 月 12 日のままなのは上流が動いて
いないから」と読める。片方だけでは、静かなことと取得できていないことを区別できない。

**``meta.json`` には相乗りさせない。** あれは同じ入力なら同じバイト列になる生成物で
(ADR 0014)、日付を入れると ``build-records`` (ローカルの全件組み立て) の出力まで
実行日で揺れる。出典表記が求める利用日と、運用の確認日は意味が違う。

置き場がルートなのは ``removed.jsonl`` と同じ理由 (ADR 0021) — ``data/`` の下に
置くと ``meta.json`` に無いファイルとして配信側に弾かれる。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from heritage_crawler.cache import atomic_write
from heritage_crawler.catalog import Dataset
from heritage_crawler.metadata import JST

SCHEMA_VERSION: Final = 1
"""``status.json`` の形式。``meta.json`` とは別に数える (器が違えば版も違う)。"""

STATUS_FILENAME: Final = "status.json"


def status_path(output_dir: Path, dataset: Dataset) -> Path:
    """``status.json`` はデータリポジトリのルートに置く (``meta.json`` と並べる)。"""
    return output_dir / dataset.repo / STATUS_FILENAME


def checked_today(now: datetime) -> str:
    """確認日を、日本時間の日付にする。

    巡回の枠も利用日も日本時間で切っている (ADR 0020 / ADR 0014)。ここだけ UTC に
    すると、日本の朝に走る週次が前日の日付を書く。

    >>> from datetime import UTC, datetime
    >>> checked_today(datetime(2026, 8, 23, 18, 0, tzinfo=UTC))
    '2026-08-24'
    >>> checked_today(datetime(2026, 8, 23, 14, 59, tzinfo=UTC))
    '2026-08-23'
    """
    return now.astimezone(JST).strftime("%Y-%m-%d")


def build_status(
    dataset: Dataset,
    checked_date: str,
    accessed_date: str,
    changed: bool,
    records: int,
    run_url: str = "",
) -> dict[str, Any]:
    """1 データセットぶんの ``status.json`` を組み立てる。

    ``accessed_date`` と ``records`` は ``meta.json`` に書いたものと同じ値を渡す。
    **数え直さない** — 2 か所で数えると、ずれたときにどちらが正しいか分からなくなる。

    ``run_url`` は空ならキーごと落とす。手元での組み立てには実行の URL が無く、
    空文字を残すと「あるはずのものが空」と読めてしまう。

    >>> from heritage_crawler.catalog import TARGET_DATASETS
    >>> build_status(TARGET_DATASETS[0], "2026-08-24", "2026-08-12", False, 1854)
    ... # doctest: +ELLIPSIS
    {'schema_version': 1, 'repo': '...', 'checked_date': '2026-08-24', ...}
    """
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repo": dataset.repo,
        "checked_date": checked_date,
        "accessed_date": accessed_date,
        "changed": changed,
        "records": records,
    }
    if run_url:
        payload["run_url"] = run_url
    return payload


def write_status(path: Path, payload: Mapping[str, Any]) -> None:
    """``status.json`` を書く。整形して置くのは、人も読む 1 枚だから。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_write(path, text.encode("utf-8"))
