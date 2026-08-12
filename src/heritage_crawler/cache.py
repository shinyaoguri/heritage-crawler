"""取得物の置き場と、取得済みを記録するマニフェスト。

生の取得物はリポジトリにコミットしない (ADR 0006 / .gitignore の cache/)。
中断しても取得済みをやり直さずに再開できるよう、1 件取るたびにマニフェストへ
書き足す。

書き方は件数で分けている。

- 台帳 (``LedgerCache``) は 153 件しかないので、毎回 JSON 全体を書き直す。
  一時ファイル経由で置き換えるため、途中で落ちてもマニフェストは壊れない
- 詳細 (``DetailCache``) は 2 万件を超える。全体を書き直すと 1 件取るたびに
  マニフェスト全体を書き直すことになるので、JSON Lines へ 1 行ずつ追記する
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from heritage_crawler.catalog import Area, Category

logger = logging.getLogger(__name__)

MANIFEST_VERSION: Final = 1
DETAIL_MANIFEST_VERSION: Final = 1
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
        self._whole_counts: dict[str, int] = {}

    @property
    def ledger_dir(self) -> Path:
        return self.root / "ledger"

    @property
    def manifest_path(self) -> Path:
        return self.ledger_dir / "manifest.json"

    def csv_path(self, category: Category, area: Area) -> Path:
        return self.ledger_dir / category.code / f"{area.code}-{area.slug}.csv"

    def recovered_csv_path(self, category: Category) -> Path:
        """地域では引けない指定を一覧から組み立て直した CSV の置き場 (ADR 0017)。

        地域別 (``<コード>-<slug>.csv``) と名前が衝突しない形にしてある。
        取得ではなく検査の産物なので、マニフェストには記録しない。
        """
        return self.ledger_dir / category.code / "recovered.csv"

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
        self._whole_counts = dict(raw.get("whole_counts", {}))
        return {key: LedgerEntry(**value) for key, value in raw.get("entries", {}).items()}

    @property
    def whole_counts(self) -> dict[str, int]:
        """分類ごとの全国件数。地域合計と突き合わせて取りこぼしを見つけるための基準。"""
        if self._entries is None:
            self._entries = self._load()
        return self._whole_counts

    def record_whole_count(self, category: Category, hit_count: int) -> None:
        self.whole_counts[category.code] = hit_count
        self._save()

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
            atomic_write(path, csv_bytes)
        self.entries[entry_key(category, area)] = entry
        self._save()

    def _save(self) -> None:
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": MANIFEST_VERSION,
            "whole_counts": dict(sorted(self._whole_counts.items())),
            "entries": {key: asdict(entry) for key, entry in sorted(self.entries.items())},
        }
        atomic_write(
            self.manifest_path,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )


@dataclass(frozen=True)
class DetailEntry:
    """詳細ページ 1 件の取得結果。

    失敗も記録する。2 万件を 1 度で完走する前提を置かないので、後からまとめて
    拾い直せる必要がある (ADR 0006)。
    """

    daichou_id: str
    kanri_taishou_id: str
    ok: bool
    byte_count: int
    """gzip する前の生 HTML のバイト数。失敗なら 0。"""

    fetched_at: str
    """ISO 8601 の UTC。"""

    error: str = ""


def detail_key(daichou_id: str, kanri_taishou_id: str) -> str:
    return f"{daichou_id}/{kanri_taishou_id}"


# ID をそのままパスに使うので、パス区切りなどが紛れ込んだら弾く。
# 実データは数字だけだが、元データの表記ゆれを取り込む口なので検査しておく。
_SAFE_ID: Final = re.compile(r"[0-9A-Za-z_-]+")


class DetailCache:
    """``cache/detail/`` 配下の読み書き。

    生 HTML は gzip で持つ。全件で約 1 GB が 100 MB 前後に収まり、パース仕様を
    変えるたびに取り直さずに済む (ADR 0006 の「取得と解析を分離する」)。
    """

    def __init__(self, root: Path = DEFAULT_CACHE_DIR) -> None:
        self.root = root
        self._entries: dict[str, DetailEntry] | None = None
        self._lock = threading.Lock()

    @property
    def detail_dir(self) -> Path:
        return self.root / "detail"

    @property
    def manifest_path(self) -> Path:
        return self.detail_dir / "manifest.jsonl"

    def html_path(self, daichou_id: str, kanri_taishou_id: str) -> Path:
        for value in (daichou_id, kanri_taishou_id):
            if not _SAFE_ID.fullmatch(value):
                raise ValueError(f"ファイル名にできない ID: {value!r}")
        return self.detail_dir / daichou_id / f"{kanri_taishou_id}.html.gz"

    @property
    def entries(self) -> dict[str, DetailEntry]:
        if self._entries is None:
            self._entries = self._load()
        return self._entries

    def _load(self) -> dict[str, DetailEntry]:
        if not self.manifest_path.exists():
            return {}
        entries: dict[str, DetailEntry] = {}
        with self.manifest_path.open(encoding="utf-8") as lines:
            first = next(lines, "")
            if not first.strip():
                return entries
            version = json.loads(first).get("version")
            if version != DETAIL_MANIFEST_VERSION:
                raise ValueError(
                    f"{self.manifest_path} のマニフェスト形式が未対応 (version={version!r})。"
                    "作り直すか変換すること"
                )
            for number, line in enumerate(lines, start=2):
                try:
                    entry = DetailEntry(**json.loads(line))
                except (json.JSONDecodeError, TypeError):
                    # 追記の途中で電源が落ちると末尾の行が欠ける。読める行だけ使い、
                    # 欠けた 1 件は未取得として取り直す。
                    logger.warning(
                        "%s の %d 行目を読めなかった。無視する", self.manifest_path, number
                    )
                    continue
                entries[detail_key(entry.daichou_id, entry.kanri_taishou_id)] = entry
        return entries

    def is_done(self, daichou_id: str, kanri_taishou_id: str) -> bool:
        """再開時に飛ばしてよいか。

        記録があっても HTML の実体が消えていれば取り直す。
        """
        entry = self.entries.get(detail_key(daichou_id, kanri_taishou_id))
        if entry is None or not entry.ok:
            return False
        return self.html_path(daichou_id, kanri_taishou_id).exists()

    def failures(self) -> list[DetailEntry]:
        """取得に失敗したまま残っているもの。同じキーは最後の結果で判断する。"""
        return [entry for entry in self.entries.values() if not entry.ok]

    def record(self, entry: DetailEntry, html: bytes | None = None) -> None:
        """HTML を保存し、マニフェストへ 1 行書き足す。並列で呼んでよい。"""
        if html is not None:
            path = self.html_path(entry.daichou_id, entry.kanri_taishou_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            # mtime を 0 に固定して、同じ HTML なら同じバイト列になるようにする。
            atomic_write(path, gzip.compress(html, mtime=0))
        with self._lock:
            self.entries[detail_key(entry.daichou_id, entry.kanri_taishou_id)] = entry
            self._append(entry)

    def _append(self, entry: DetailEntry) -> None:
        self.detail_dir.mkdir(parents=True, exist_ok=True)
        first = not self.manifest_path.exists()
        with self.manifest_path.open("a", encoding="utf-8") as manifest:
            if first:
                manifest.write(json.dumps({"version": DETAIL_MANIFEST_VERSION}) + "\n")
            manifest.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    def read_html(self, daichou_id: str, kanri_taishou_id: str) -> bytes:
        """保存した生 HTML を読み戻す (解析層はキャッシュだけを見る)。"""
        return gzip.decompress(self.html_path(daichou_id, kanri_taishou_id).read_bytes())


def atomic_write(path: Path, data: bytes) -> None:
    """一時ファイル経由で置き換える。途中で落ちても中途半端な内容を残さない。"""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
