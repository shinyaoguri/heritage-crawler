"""差分更新層 — 前回の状態と突き合わせ、取り直すぶんだけを決める (ADR 0018 / ADR 0020)。

**前回の状態は 2 つある。**

- データリポジトリの JSON Lines … 行そのもの。追加と削除はこれと台帳を比べて決める
- 前回の台帳 CSV … **値が変わったかはこちらと比べる** (ADR 0020)。同じ台帳どうしなら
  18 列を素直に比べられる

生 HTML は週次実行へ持ち回らない (Actions のキャッシュは 7 日で消え、リポジトリにも
コミットしない。ADR 0006)。台帳 CSV だけは artifact で持ち回る。

取り直すのは 3 種類だけ。

1. 台帳に現れた新しいキー (新規指定)
2. 前回の台帳と値が食い違うキー
3. 巡回のぶん — 全体の 1/52

3 が要るのは、**ソース側に更新日が無く、詳細ページだけの項目 (解説文・員数・
構造及び形式等など) の変更は取り直して比べるしか捕まえられない**ため。
全件を毎回取り直すのは相手先に対して重いので、52 週で一巡させる。

台帳から消えたキーは指定解除として落とすが、**その分類の網羅性が確かめられて
いるときに限る** (``CategorySummary.looks_complete``)。取得の失敗や相手側の
一時的な障害を指定解除と誤認して行を消さないため。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Final

from heritage_crawler.catalog import CATEGORIES_BY_CODE, Category, Dataset
from heritage_crawler.detail import Presence, Target
from heritage_crawler.export import REMOVED_FILENAME, Reuse, removal_entry
from heritage_crawler.ledger import EXPECTED_CSV_HEADER, LedgerRow, read_csv_rows
from heritage_crawler.metadata import METADATA_FILENAME, accessed_date
from heritage_crawler.record import category_of

logger = logging.getLogger(__name__)

ROTATION_SLOTS: Final = 52
"""巡回を一巡させる週数。1 週あたり全体の 1/52 (約 460 件) を取り直す (ADR 0020)。

一巡に 1 年かかるのは月次のとき (1/12 × 12 か月) と同じ。**相手先への総量は
変えずに、毎週へならしただけ**。
"""


class UpdateError(RuntimeError):
    """前回の状態を読めない。差分の基準が無いまま進めない。"""


def rotation_slot(key: str, slots: int = ROTATION_SLOTS) -> int:
    """``(台帳ID, 管理対象ID)`` を巡回の枠に割り当てる (ADR 0020)。

    ハッシュで決めるので、都道府県にも種別にも取得順にも偏らない。**実行ごとに
    同じ答えを返す必要がある**ので、実行のたびに種が変わる組み込みの ``hash`` は
    使わない。

    >>> rotation_slot("401/00003452")
    5
    >>> 0 <= rotation_slot("101/00015157") < 52
    True
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % slots


@dataclass(frozen=True)
class LedgerDiff:
    """前回の台帳と今回の台帳の差 (ADR 0020)。

    **比べるのは CSV 同士。** 前は CSV の値と出力 (JSON Lines) の値を比べており、
    意味が揃う列だけを選ぶ・分類ごとに読み替える・CSV 側が空の欄は飛ばす、といった
    近似が要った。同じ台帳どうしなら 18 列を素直に比べられる。

    **追加と削除はここで決めない。** 台帳と出力の突き合わせ (``plan_update``) が
    受け持つ — 前回の台帳が無い回でもそちらは働くし、地域をまたいで移った行を
    「片方で消えてもう片方に現れた」と読み違えずに済む。
    """

    changed: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """``(台帳ID, 管理対象ID)`` → 値が変わった列の名前。"""

    changed_files: tuple[str, ...] = ()
    """バイト列が違った CSV。報告に出して、どこが動いたかを読めるようにする。"""

    compared_files: int = 0


def compare_ledgers(
    previous_dir: Path, ledger_dir: Path, categories: Sequence[Category]
) -> LedgerDiff:
    """前回の台帳 (``previous_dir``) と今回 (``ledger_dir``) を突き合わせる。

    **まずファイルごとにバイトで比べる。** 同じなら中身を読むまでもない。相手先の
    並び順が揺れても、違うと出たファイルの行をキーで突き合わせるので差分にはならない。
    """
    before_files = _csv_files(previous_dir, categories)
    after_files = _csv_files(ledger_dir, categories)

    changed_files: list[str] = []
    before_rows: dict[str, LedgerRow] = {}
    after_rows: dict[str, LedgerRow] = {}
    names = sorted(before_files.keys() | after_files.keys())
    for name in names:
        before, after = before_files.get(name), after_files.get(name)
        if before and after and before.read_bytes() == after.read_bytes():
            continue
        changed_files.append(name)
        category = CATEGORIES_BY_CODE[name.split("/", 1)[0]]
        _collect(before, category, before_rows)
        _collect(after, category, after_rows)

    changed: dict[str, tuple[str, ...]] = {}
    for key, row in after_rows.items():
        old = before_rows.get(key)
        # 前回どこにも無かった行は「追加」。ここでは扱わない (上のクラス注記)。
        if old is None:
            continue
        if columns := _changed_columns(old, row):
            changed[key] = columns

    logger.info(
        "台帳の突き合わせ: %d ファイル中 %d ファイルが変化 / 値が変わった指定 %d 件",
        len(names),
        len(changed_files),
        len(changed),
    )
    return LedgerDiff(
        changed=changed, changed_files=tuple(changed_files), compared_files=len(names)
    )


def _csv_files(ledger_dir: Path, categories: Sequence[Category]) -> dict[str, Path]:
    """``<分類コード>/<ファイル名>`` → パス。回収ぶん (ADR 0017) も同じ並びに入る。"""
    found: dict[str, Path] = {}
    for category in categories:
        directory = ledger_dir / category.code
        for path in sorted(directory.glob("*.csv")) if directory.is_dir() else []:
            found[f"{category.code}/{path.name}"] = path
    return found


def _collect(path: Path | None, category: Category, into: dict[str, LedgerRow]) -> None:
    """CSV の行をキーで引ける形に足す。

    同じ棟が複数の地域の CSV に出ることがある (102 の琵琶湖疏水施設)。値は同じ
    はずなので、後から来たもので上書きしてよい。
    """
    if path is None or not path.exists():
        return
    for values in read_csv_rows(path.read_bytes()):
        if len(values) != len(EXPECTED_CSV_HEADER):
            continue
        row = LedgerRow(category=category, values=tuple(values))
        into[row.key] = row


def _changed_columns(before: LedgerRow, after: LedgerRow) -> tuple[str, ...]:
    """食い違う列の名前。**18 列すべてを素直に比べる** (同じ台帳どうしなので)。"""
    return tuple(
        column
        for index, column in enumerate(EXPECTED_CSV_HEADER)
        if before.values[index] != after.values[index]
    )


@dataclass(frozen=True)
class Existing:
    """前回の出力 (データリポジトリの JSON Lines と ``meta.json``)。"""

    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    """``(台帳ID, 管理対象ID)`` → レコード。

    401 の複合指定は 2 つのリポジトリに同じ行が出るので (ADR 0012)、キーで畳む。
    """

    repos: dict[str, set[str]] = field(default_factory=dict)
    """``(台帳ID, 管理対象ID)`` → 前回そのキーが居たリポジトリ名。

    ``records`` はキーで畳むので居場所が消える。**落とした行をどの
    ``removed.jsonl`` へ書くか**と、振り分けが変わった行の検出に要る (ADR 0021)。
    """

    labels: dict[str, dict[str, str]] = field(default_factory=dict)
    """リポジトリ名 → ``meta.json`` の表示名。"""

    accessed_dates: dict[str, str] = field(default_factory=dict)
    """リポジトリ名 → ``meta.json`` の利用日。行が動かなかった回に据え置く (ADR 0020)。"""

    removed: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    """リポジトリ名 → キー → 前回の ``removed.jsonl`` の 1 行 (ADR 0021)。

    **前回の記録をそのまま引き継ぐ**ために読む。消えたままの週に書き直すと
    ``missing_since`` が動いて「いつ消えたか」が失われ、記録が毎週揺れる。
    """

    files: int = 0


def record_key(record: Mapping[str, Any]) -> str:
    return f"{record['ledger_id']}/{record['managed_id']}"


def read_existing(output_dir: Path, datasets: Sequence[Dataset]) -> Existing:
    """データリポジトリから前回の状態を読み戻す。

    **読めない行は例外にする。** 黙って飛ばすと、その行は「台帳にあって手元に
    無い」= 新規として扱われ、悪くすると指定解除として消える。差分の基準が
    壊れているときは進まない方がよい。
    """
    records: dict[str, dict[str, Any]] = {}
    repos: dict[str, set[str]] = {}
    labels: dict[str, dict[str, str]] = {}
    accessed: dict[str, str] = {}
    removed: dict[str, dict[str, dict[str, Any]]] = {}
    files = 0
    for dataset in datasets:
        directory = output_dir / dataset.repo / "data"
        for path in sorted(directory.glob("*.jsonl")) if directory.is_dir() else []:
            files += 1
            for number, key, record in _read_jsonl(path):
                try:
                    # 分類を戻せないと、消えたときの扱いも書き先も決まらない。
                    category_of(record)
                except KeyError as error:
                    raise UpdateError(
                        f"{path} の {number} 行目から分類を戻せない: "
                        f"分類コード {record.get('category_code')!r} / "
                        f"台帳ID {record.get('ledger_id')!r}"
                    ) from error
                records.setdefault(key, record)
                repos.setdefault(key, set()).add(dataset.repo)
        meta = _read_metadata(output_dir / dataset.repo / METADATA_FILENAME)
        labels[dataset.repo] = {
            str(key): str(value) for key, value in meta.get("labels", {}).items()
        }
        if date := meta.get("source", {}).get("accessed_date"):
            accessed[dataset.repo] = str(date)
        path = output_dir / dataset.repo / REMOVED_FILENAME
        removed[dataset.repo] = (
            {key: entry for _, key, entry in _read_jsonl(path)} if path.exists() else {}
        )
    logger.info(
        "前回の出力を読んだ: %d 件 / %d ファイル (削除の記録 %d 件)",
        len(records),
        files,
        sum(len(entries) for entries in removed.values()),
    )
    return Existing(
        records=records,
        repos=repos,
        labels=labels,
        accessed_dates=accessed,
        removed=removed,
        files=files,
    )


def _read_jsonl(path: Path) -> list[tuple[int, str, dict[str, Any]]]:
    """JSON Lines を ``(行番号, キー, 行)`` で読む。読めない行は例外にする。

    **黙って飛ばさない。** 出力の行なら「台帳にあって手元に無い」= 新規に化け、
    削除の記録なら消えた行が記録から落ちる。どちらも差分の基準が壊れている。
    """
    found: list[tuple[int, str, dict[str, Any]]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            found.append((number, record_key(record), record))
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise UpdateError(f"{path} の {number} 行目を読めない: {error}") from error
    return found


def _read_metadata(path: Path) -> dict[str, Any]:
    """前回の ``meta.json``。無ければ空 (今回組み立てたぶんから作り直される)。"""
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise UpdateError(f"{path} を読めない: {error}") from error
    return payload if isinstance(payload, dict) else {}


class Reason(Enum):
    """なぜ詳細ページを取り直すか。"""

    ADDED = "新規"
    CHANGED = "台帳の値が変わった"
    ROTATED = "巡回"


@dataclass(frozen=True)
class Refetch:
    """取り直す 1 件と、その理由。"""

    target: Target
    reason: Reason
    changes: tuple[str, ...] = ()
    """``CHANGED`` のときに食い違った欄。報告に出して、原因を追えるようにする。"""

    @property
    def key(self) -> str:
        return self.target.key


@dataclass
class UpdatePlan:
    """今月どこへ手を入れるか (ADR 0018)。"""

    slot: int
    refetch: list[Refetch] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    """台帳から消え、出力からも落とすキー (指定解除)。"""

    retained: list[str] = field(default_factory=list)
    """台帳から消えたが、確かめられないので残すキー。"""

    unchanged: int = 0

    complete: frozenset[str] = frozenset()
    """網羅性を確かめられた分類コード。落とすかどうかの判断に後から使う。"""

    control: Target | None = None
    """存在確認の対照群 — **実在すると分かっているキー** (ADR 0021)。

    台帳にある行から決める。キーの小さいものを選ぶのは、取得順にも都道府県にも
    寄らせないため (毎週同じ 1 件になる)。
    """

    added: dict[tuple[str, str, str], str] = field(default_factory=dict)
    """新規指定の同定情報 → キー。**格上げを削除と読み違えない**ために持つ。

    101 が 102 に指定されると登録は抹消され、台帳ID が変わって別レコードになる
    (ADR 0021)。手元からは「101 から消えて 102 に現れた」と見える。
    """

    presence: dict[str, Presence] = field(default_factory=dict)
    """キー → 詳細ページに問い合わせた結果 (``verify_removals`` が入れる)。"""

    @property
    def targets(self) -> list[Target]:
        return [item.target for item in self.refetch]

    def counts(self) -> dict[Reason, int]:
        return {
            reason: sum(1 for item in self.refetch if item.reason is reason) for reason in Reason
        }

    @property
    def is_empty(self) -> bool:
        """手を入れるところが 1 つも無いか。"""
        return not (self.refetch or self.removed)


def plan_update(
    existing: Existing,
    rows: Iterable[LedgerRow],
    *,
    slot: int,
    complete_categories: Collection[str],
    diff: LedgerDiff | None = None,
) -> UpdatePlan:
    """前回の出力と台帳を突き合わせて、今週の計画を立てる。

    ``slot`` は巡回の枠 (0〜51)、``complete_categories`` は網羅性が確かめられた
    分類コード。**そこに無い分類のキーは、台帳から消えていても落とさない**。

    ``diff`` は前回の台帳との突き合わせ (``compare_ledgers``)。**無くても働く** —
    追加と削除は台帳と出力を比べれば分かる。落ちるのは「台帳の値が変わった」だけで、
    それも巡回でいずれ拾う。
    """
    plan = UpdatePlan(slot=slot, complete=frozenset(complete_categories))
    changed = diff.changed if diff else {}
    seen: set[str] = set()

    for row in rows:
        if row.key in seen:  # 同じ棟が複数の地域の CSV に出る (ADR 0008)
            continue
        seen.add(row.key)
        target = Target(
            category_code=row.category.code,
            kanri_taishou_id=row.get("管理対象ID"),
            name=" ".join(part for part in (row.get("名称"), row.get("棟名")) if part),
        )
        if plan.control is None or row.key < plan.control.key:
            plan.control = target
        record = existing.records.get(row.key)
        if record is None:
            plan.refetch.append(Refetch(target, Reason.ADDED))
            if identity := _identity(target.name, row.get("緯度"), row.get("経度")):
                plan.added[identity] = row.key
        elif columns := changed.get(row.key):
            plan.refetch.append(Refetch(target, Reason.CHANGED, columns))
        elif rotation_slot(row.key) == slot:
            plan.refetch.append(Refetch(target, Reason.ROTATED))
        else:
            plan.unchanged += 1

    for key in existing.records.keys() - seen:
        code = str(existing.records[key]["ledger_id"])
        (plan.removed if code in complete_categories else plan.retained).append(key)
    plan.removed.sort()
    plan.retained.sort()
    return plan


CONCLUSIONS: Final[dict[Presence, str]] = {
    Presence.GONE: "delisted",
    Presence.ALIVE: "unlisted",
    Presence.UNKNOWN: "unverified",
}
"""存在確認の結果から出る結論 (ADR 0021)。

``unverified`` は「確かめていない」。台帳からの不在は*消極的*な証拠でしかないので、
積極的な返答を得ていない回はそう書く。``unlisted`` は「台帳から外れただけで
データベースにはまだ居る」。
"""

RECLASSIFIED: Final = "reclassified"
"""同じものが別の分類に現れた = 格上げ・格下げ。**削除ではない** (ADR 0021)。"""


def candidates(plan: UpdatePlan, existing: Existing) -> list[Target]:
    """存在を確かめる対象。**落とす候補と残す候補の両方**を返す。

    残す側も確かめるのは、**102 では網羅性を確かめられない**ため — 全国件数と
    キーの異なり数を直接比べられる分類ではないので、詳細ページの返答が唯一の
    個別証拠になる (ADR 0021)。
    """
    found = []
    for key in sorted(set(plan.removed) | set(plan.retained)):
        record = existing.records.get(key, {})
        _, _, managed_id = key.partition("/")
        # 出力レコードのキーは台帳ID ベースだが、詳細ページは分類コードで引く
        # (#74)。行が名乗る分類コードから戻す (ADR 0024)。
        found.append(Target(category_of(record).code, managed_id, str(record.get("name", ""))))
    return found


def verify_removals(plan: UpdatePlan, presence: Mapping[str, Presence]) -> None:
    """存在確認の結果で、落とすかどうかを決め直す (ADR 0021)。

    **確かめられなかったものは落とさない。** 混んでいる時間帯に当たった 1 件を
    指定解除と読み違えないため — 翌週やり直せば済む。逆に「無い」と答えたものは、
    網羅性を確かめられない分類でも落とす (それ自体が個別の証拠なので)。
    """
    keep: list[str] = []
    drop: list[str] = []
    for key in sorted(set(plan.removed) | set(plan.retained)):
        found = presence.get(key, Presence.UNKNOWN)
        complete = key.partition("/")[0] in plan.complete
        gone = found is Presence.GONE or (found is Presence.ALIVE and complete)
        (drop if gone else keep).append(key)
    plan.removed, plan.retained, plan.presence = drop, keep, dict(presence)


def _identity(name: str, latitude: Any, longitude: Any) -> tuple[str, str, str] | None:
    """同じ文化財だと言い切れる組み合わせ (ADR 0021)。

    **名称だけでは弱い** — 「本堂」は何十とある。緯度経度を添えて完全一致だけを
    採る。座標が無い行は同定しない (取りこぼす側に倒す)。
    """
    try:
        coordinates = (f"{float(latitude):.6f}", f"{float(longitude):.6f}")
    except (TypeError, ValueError):
        return None
    return (name, *coordinates) if name else None


def _record_identity(record: Mapping[str, Any]) -> tuple[str, str, str] | None:
    name = " ".join(
        str(part) for part in (record.get("name", ""), record.get("ridge_name", "")) if part
    )
    return _identity(name, record.get("latitude"), record.get("longitude"))


def _removals(plan: UpdatePlan, existing: Existing, today: str) -> dict[str, dict[str, Any]]:
    """落としたキー → ``removed.jsonl`` の 1 行。

    ``plan.retained`` は入れない — あれは「消したかどうかを断じられない」一時的な
    状態で、平時は 0 件、取得が転んだ週にだけ現れて翌週消える。削除されていない行を
    削除リストに書くと名前と実態がずれ、恒久ファイルが毎週揺れる (ADR 0021)。
    """
    entries: dict[str, dict[str, Any]] = {}
    for key in plan.removed:
        record = existing.records[key]
        found = plan.presence.get(key, Presence.UNKNOWN)
        identity = _record_identity(record)
        moved = plan.added.get(identity) if identity else None
        # 利用日は「そのデータを取り出した日」なので、前回の meta.json が
        # そのまま「最後に台帳で見た日」になる (ADR 0014 / ADR 0020)。
        seen = (
            existing.accessed_dates.get(repo, "") for repo in sorted(existing.repos.get(key, ()))
        )
        entries[key] = removal_entry(
            record,
            conclusion=RECLASSIFIED if moved else CONCLUSIONS[found],
            last_seen_date=max(seen, default=""),
            missing_since=today,
            category_complete=key.partition("/")[0] in plan.complete,
            detail_page=found.value,
            matched_elsewhere=[moved] if moved else None,
        )
    return entries


def reuse_for(plan: UpdatePlan, existing: Existing, accessed_at: str) -> Reuse:
    """出力層へ渡す「使い回すぶん」を作る (ADR 0018)。

    **落とすキーだけを外す。** 残りは全部渡し、キャッシュに新しい詳細があるかは
    出力層が見る — 取り直したはずの 1 件が取得に失敗しても、前回の行が残って
    行の消失にはならない。

    落としたキーは捨てずに ``removals`` として渡す (ADR 0021)。どのリポジトリの
    ``removed.jsonl`` へ書くかは前回の居場所で決まるので、``repos`` も一緒に渡す。
    """
    dropped = set(plan.removed)
    records = {key: record for key, record in existing.records.items() if key not in dropped}
    return Reuse(
        records=records,
        retained=[records[key] for key in plan.retained],
        labels=existing.labels,
        accessed_at=accessed_at,
        accessed_dates=existing.accessed_dates,
        removals=_removals(plan, existing, accessed_date(accessed_at)),
        previous_repos=existing.repos,
        previous_removed=existing.removed,
    )


_EXAMPLES: Final = 10
"""報告に並べる実例の数。件数だけでは原因を追えないので、少しだけ名指しする。"""


def format_ledger_diff(diff: LedgerDiff) -> str:
    """台帳の突き合わせを人が読める形にする。**どのファイルが動いたかまで出す。**"""
    lines = [
        f"台帳 {diff.compared_files:,} ファイル中 "
        f"{len(diff.changed_files):,} ファイルが前回と違う"
    ]
    lines.extend(f"  {name}" for name in diff.changed_files[:_EXAMPLES])
    if len(diff.changed_files) > _EXAMPLES:
        lines.append(f"  ほか {len(diff.changed_files) - _EXAMPLES:,} ファイル")
    lines.append(f"値が変わった指定 {len(diff.changed):,} 件")
    for key, columns in list(diff.changed.items())[:_EXAMPLES]:
        lines.append(f"  {key} ({'・'.join(columns)})")
    if len(diff.changed) > _EXAMPLES:
        lines.append(f"  ほか {len(diff.changed) - _EXAMPLES:,} 件")
    return "\n".join(lines)


def format_verification(plan: UpdatePlan) -> str:
    """存在確認の結果を人が読める形にする (ADR 0021)。

    **落とさなかったものまで出す。** 確かめられない週が続いていることに気付けないと、
    消えたはずの行がいつまでも残っていても分からない。
    """
    counts = {state: 0 for state in Presence}
    for state in plan.presence.values():
        counts[state] += 1
    lines = [
        "存在を確かめた: "
        + " / ".join(f"{state.value} {counts[state]:,}" for state in Presence)
        + f" (対照群 {plan.control.key if plan.control else '-'})"
    ]
    if plan.removed:
        lines.append(f"落とす {len(plan.removed):,} 件")
        lines.extend(
            f"  {key} ({plan.presence.get(key, Presence.UNKNOWN).value})"
            for key in plan.removed[:_EXAMPLES]
        )
    if plan.retained:
        lines.append(f"確かめられず残す {len(plan.retained):,} 件 (翌週やり直す)")
        lines.extend(f"  {key}" for key in plan.retained[:_EXAMPLES])
    return "\n".join(lines)


def format_plan(plan: UpdatePlan, existing: Existing) -> str:
    """計画を人が読める形にする。件数だけでなく、なぜ取り直すかまで出す。"""
    counts = plan.counts()
    lines = [
        f"前回の出力 {len(existing.records):,} 件 / 巡回の枠 {plan.slot + 1}/{ROTATION_SLOTS}",
        f"取り直す {len(plan.refetch):,} 件 = "
        + " / ".join(f"{reason.value} {counts[reason]:,}" for reason in Reason),
        f"そのまま {plan.unchanged:,} 件",
    ]
    if plan.removed:
        lines.append(f"落とす (指定解除) {len(plan.removed):,} 件")
        lines.extend(f"  {key}" for key in plan.removed[:_EXAMPLES])
        if len(plan.removed) > _EXAMPLES:
            lines.append(f"  ほか {len(plan.removed) - _EXAMPLES:,} 件")
    if plan.retained:
        lines.append(
            f"台帳から消えたが残す {len(plan.retained):,} 件 "
            "(その分類の網羅性を確かめられていない)"
        )
        lines.extend(f"  {key}" for key in plan.retained[:_EXAMPLES])
    changed = [item for item in plan.refetch if item.reason is Reason.CHANGED]
    if changed:
        lines.append("台帳の値が変わったもの:")
        lines.extend(
            f"  {item.key} {item.target.name} ({'・'.join(item.changes)})"
            for item in changed[:_EXAMPLES]
        )
        if len(changed) > _EXAMPLES:
            lines.append(f"  ほか {len(changed) - _EXAMPLES:,} 件")
    return "\n".join(lines)
