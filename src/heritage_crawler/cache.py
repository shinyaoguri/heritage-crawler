"""取得した台帳 CSV の置き場と、取得済みを記録するマニフェスト。

生の取得物はリポジトリにコミットしない (ADR 0006 / .gitignore の cache/)。
中断しても取得済みをやり直さずに再開できるよう、1 件取るたびにマニフェストを
書き換える。書き換えは一時ファイル経由の置き換えで行い、途中で落ちても
マニフェストが壊れないようにする。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from heritage_crawler.catalog import Area, Category

MANIFEST_VERSION: Final = 1
DEFAULT_CACHE_DIR: Final = Path("cache")


@dataclass(frozen=True)
class LedgerEntry:
    """(分類 × 地域) を 1 件取得した記録。"""

    category_code: str
    area_name: str
    hit_count: int
    """検索結果の件数。**指定**単位。"""

    row_count: int
    """CSV の行数 (ヘッダを除く)。**棟**単位。"""

    byte_count: int
    fetched_at: str
    """ISO 8601 の UTC。"""


def entry_key(category: Category, area: Area) -> str:
    return f"{category.code}/{area.code}-{area.slug}"


class LedgerCache:
    """``cache/ledger/`` 配下の読み書き。"""

    def __init__(self, root: Path = DEFAULT_CACHE_DIR) -> None:
        self.root = root
        self._entries: dict[str, LedgerEntry] | None = None

    @property
    def ledger_dir(self) -> Path:
        return self.root / "ledger"

    @property
    def manifest_path(self) -> Path:
        return self.ledger_dir / "manifest.json"

    def csv_path(self, category: Category, area: Area) -> Path:
        return self.ledger_dir / category.code / f"{area.code}-{area.slug}.csv"

    @property
    def entries(self) -> dict[str, LedgerEntry]:
        if self._entries is None:
            self._entries = self._load()
        return self._entries

    def _load(self) -> dict[str, LedgerEntry]:
        if not self.manifest_path.exists():
            return {}
        raw: Any = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        version = raw.get("version")
        if version != MANIFEST_VERSION:
            raise ValueError(
                f"{self.manifest_path} のマニフェスト形式が未対応 (version={version!r})。"
                "作り直すか変換すること"
            )
        return {key: LedgerEntry(**value) for key, value in raw.get("entries", {}).items()}

    def is_done(self, category: Category, area: Area) -> bool:
        """再開時に飛ばしてよいか。

        マニフェストに記録があっても CSV の実体が消えていれば取り直す
        (0 件のときは CSV を作らないので、記録だけで済ませる)。
        """
        entry = self.entries.get(entry_key(category, area))
        if entry is None:
            return False
        return entry.row_count == 0 or self.csv_path(category, area).exists()

    def record(self, category: Category, area: Area, entry: LedgerEntry, csv_bytes: bytes) -> None:
        """CSV を保存し、マニフェストへ 1 件書き足す。"""
        if csv_bytes:
            path = self.csv_path(category, area)
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(path, csv_bytes)
        self.entries[entry_key(category, area)] = entry
        self._save()

    def _save(self) -> None:
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": MANIFEST_VERSION,
            "entries": {key: asdict(entry) for key, entry in sorted(self.entries.items())},
        }
        _atomic_write(
            self.manifest_path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
