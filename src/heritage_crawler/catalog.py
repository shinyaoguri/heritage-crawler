"""データベースの語彙 (分類・検索の地域) と、詳細ページ URL の組み立て。

ここにある値と URL 形式は 2026-08-11 に実データベースへアクセスして確認したもの
(ADR 0002 と Issue #1 の調査所見コメントを参照)。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final

BASE_URL: Final = "https://kunishitei.bunka.go.jp"


class AreaScope(Enum):
    """その分類を検索するとき、``seat_pref`` に何を送れるか (2026-08-23 実測)。

    分割軸は分類ごとに違う。都道府県で引ける分類ばかりではない — 無形文化財は
    地域の 9 区分しか持たず、選定保存技術には地域欄そのものが無い。
    """

    PREFECTURE = "prefecture"
    """47 都道府県 + ``２県以上`` + ``地域を定めない``。

    101 / 102 / 103 / 201 / 211 / 202 / 301 / 311 / 401 / 411 / 412 / 901。
    未正規化の値 (``IRREGULAR_AREAS``) もここに含める。
    """

    PREFECTURE_AND_REGION = "prefecture-and-region"
    """都道府県に加えて地域の 9 区分も option にある。302 / 322 / 312 / 323。"""

    REGION = "region"
    """地域の 9 区分だけ。

    **いまこの軸を使う分類は無い。** 303 / 313 の select には 9 区分が並ぶが、
    データ側の地域欄は全件空で 1 件も引けなかった (2026-08-23 実測)。
    option があることと引けることは別なので、語彙としては残す。
    """

    WHOLE = "whole"
    """地域で分けずに全国を 1 回で取る。

    次の 2 つがここに来る (2026-08-23 実測)。**どちらも全国 CSV が 2,000 件未満**で、
    504 にならないことを確かめてある。

    - 地域欄そのものが無い … 304 (選定保存技術)
    - 地域欄はあるが埋まっていない … 303 / 313 / 323 (全件空)、312 (662 件中 18 件が空)
    """


@dataclass(frozen=True)
class Category:
    """文化財分類。code は検索フォームの register_sub_id。

    **CSV の台帳ID とは限らない。** 台帳ID には複数の分類が同居する (#74)。
    """

    code: str
    name: str
    known_designation_count: int
    """2026-08-11 に実測した指定件数。

    棟ではなく**指定**の件数で、検索結果の件数表示と同じ単位。新規指定・解除で
    増減するため厳密一致の検査には使えないが、取得の欠損を見つける目安になる。
    """

    expands_to_buildings: bool = False
    """1 つの指定が CSV で複数の棟に展開されるか (2026-08-11 の実測)。

    展開されない分類では CSV の ``(台帳ID, 管理対象ID)`` の異なり数がそのまま
    指定件数になるので、全国件数と直接突き合わせられる (``ledger.summarize``)。
    102 だけは 1 指定あたり約 2.5 棟に展開され、単位が違うので比べられない。
    """

    area_scope: AreaScope = AreaScope.PREFECTURE
    """検索の分割軸 (``search_areas`` が実際の地域に開く)。"""

    ledger_id: str = ""
    """CSV の ``台帳ID`` 列に入る値。**空なら分類コードと同じ** (``__post_init__``)。

    台帳ID には複数の分類が同居する (#74)。401 に 401 / 411 / 412、303 に
    303 / 323 / 313 など。取得も詳細ページも分類コードで回すが、一覧から台帳の行を
    組み立て直すとき (``listing.recover_missing``) だけは台帳ID が要る。
    """

    def __post_init__(self) -> None:
        if not self.ledger_id:
            object.__setattr__(self, "ledger_id", self.code)


# 取得対象は検索フォームの register_sub_id が提供する全 19 分類 (ADR 0025)。
# 名前は原文のラベルをそのまま写す。件数は 2026-08-11 / 2026-08-23 の実測。

# 有形文化財 (建造物)
REGISTERED: Final = Category("101", "登録有形文化財（建造物）", 14748)
DESIGNATED: Final = Category("102", "国宝・重要文化財（建造物）", 2633, expands_to_buildings=True)

# 有形文化財 (美術工芸品)。台帳ID は 201 に 201 / 211 が同居する。
FINE_ARTS: Final = Category(
    "201", "国宝・重要文化財（美術工芸品）", 10954, expands_to_buildings=True
)
REGISTERED_FINE_ARTS: Final = Category("211", "登録有形文化財（美術工芸品）", 18, ledger_id="201")
REGISTERED_ART: Final = Category("202", "登録美術品", 40)

# 民俗文化財 (有形)。台帳ID は 301。
TANGIBLE_FOLK: Final = Category("301", "重要有形民俗文化財", 229)
REGISTERED_TANGIBLE_FOLK: Final = Category("311", "登録有形民俗文化財", 56, ledger_id="301")

# 民俗文化財 (無形)。台帳ID は 302。地域の 9 区分も分割軸に持つ。
INTANGIBLE_FOLK: Final = Category(
    "302", "重要無形民俗文化財", 338, area_scope=AreaScope.PREFECTURE_AND_REGION
)
REGISTERED_INTANGIBLE_FOLK: Final = Category(
    "322", "登録無形民俗文化財", 9, area_scope=AreaScope.PREFECTURE_AND_REGION, ledger_id="302"
)
DOCUMENTED_INTANGIBLE_FOLK: Final = Category(
    "312",
    "記録作成等の措置を講ずべき無形の民俗文化財",
    662,
    area_scope=AreaScope.WHOLE,
    ledger_id="302",
)

# 無形文化財。台帳ID は 303。**303 と 313 は都道府県では引けない**。
INTANGIBLE: Final = Category("303", "重要無形文化財", 100, area_scope=AreaScope.WHOLE)
REGISTERED_INTANGIBLE: Final = Category(
    "323", "登録無形文化財", 7, area_scope=AreaScope.WHOLE, ledger_id="303"
)
DOCUMENTED_INTANGIBLE: Final = Category(
    "313",
    "記録作成等の措置を講ずべき無形文化財",
    132,
    area_scope=AreaScope.WHOLE,
    ledger_id="303",
)

# 選定保存技術。**地域欄そのものが無い** ので全国を 1 回で取る。
CONSERVATION_TECHNIQUES: Final = Category(
    "304", "選定保存技術", 82, area_scope=AreaScope.WHOLE
)

# 伝統的建造物群
SELECTED: Final = Category("103", "重要伝統的建造物群保存地区", 126)

# 記念物・文化的景観。台帳ID は 401 に 401 / 411 / 412 が同居する。
MONUMENTS: Final = Category("401", "史跡名勝天然記念物", 3281)
"""記念物 (ADR 0012)。1 分類に 6 種別が同居し、棟には展開されない。"""

REGISTERED_MONUMENTS: Final = Category("411", "登録記念物", 148, ledger_id="401")
CULTURAL_LANDSCAPES: Final = Category("412", "重要文化的景観", 74, ledger_id="401")

# 世界遺産。**他分類と別軸** で、同じ物件が重複して現れる (ADR 0025)。
WORLD_HERITAGE: Final = Category("901", "世界遺産", 21)

TARGET_CATEGORIES: Final[tuple[Category, ...]] = (
    REGISTERED,
    DESIGNATED,
    FINE_ARTS,
    REGISTERED_FINE_ARTS,
    REGISTERED_ART,
    TANGIBLE_FOLK,
    REGISTERED_TANGIBLE_FOLK,
    INTANGIBLE_FOLK,
    REGISTERED_INTANGIBLE_FOLK,
    DOCUMENTED_INTANGIBLE_FOLK,
    INTANGIBLE,
    REGISTERED_INTANGIBLE,
    DOCUMENTED_INTANGIBLE,
    CONSERVATION_TECHNIQUES,
    SELECTED,
    MONUMENTS,
    REGISTERED_MONUMENTS,
    CULTURAL_LANDSCAPES,
    WORLD_HERITAGE,
)

CATEGORIES_BY_CODE: Final[dict[str, Category]] = {
    category.code: category for category in TARGET_CATEGORIES
}
"""分類コードから分類を引く。

**台帳ID からは引けない。** 台帳ID には複数の分類が同居するので (#74)、
出力レコードから分類を戻すときは ``record.category_of`` を通す (ADR 0024)。
台帳キャッシュの置き場 (``<分類コード>/<地域>.csv``) からは引ける。
"""


@dataclass(frozen=True)
class Dataset:
    """出力先のデータリポジトリ 1 つ (ADR 0009 / ADR 0012)。

    分類とリポジトリは 1:1 ではない。102 は国宝と重要文化財の 2 つに、
    401 は史跡・名勝・天然記念物とその特別指定の 6 つに分かれる。
    """

    repo: str
    """リポジトリ名。出力ディレクトリ配下のディレクトリ名も兼ねる。"""

    name: str
    category: Category

    kinds: tuple[str, ...] = ()
    """このリポジトリが受ける区分 (国宝・重文区分 / 記念物の種別)。

    空は同じ分類の残り全部を受ける**受け皿**。区分が読めなかったものもそこへ来る。
    受け皿を置かない分類 (401) では、区分が読めなければどこへも書かず報告に出す。
    """


# ADR 0009 / ADR 0012 の対応表が正本。区分を持つものを先に置き、残りを受け皿へ落とす。
# 特別◯◯ は ◯◯ のうちから指定されるが、国宝・重文と同じく排他に振り分ける。
TARGET_DATASETS: Final[tuple[Dataset, ...]] = (
    Dataset("registered-tangible-cultural-properties", "登録有形文化財（建造物）", REGISTERED),
    Dataset("national-treasures", "国宝（建造物）", DESIGNATED, kinds=("国宝",)),
    Dataset("important-cultural-properties", "重要文化財（建造物）", DESIGNATED),
    Dataset(
        "important-preservation-districts-for-groups-of-traditional-buildings",
        "重要伝統的建造物群保存地区",
        SELECTED,
    ),
    Dataset("special-historic-sites", "特別史跡", MONUMENTS, kinds=("特別史跡",)),
    Dataset("historic-sites", "史跡", MONUMENTS, kinds=("史跡",)),
    Dataset("special-places-of-scenic-beauty", "特別名勝", MONUMENTS, kinds=("特別名勝",)),
    Dataset("places-of-scenic-beauty", "名勝", MONUMENTS, kinds=("名勝",)),
    Dataset("special-natural-monuments", "特別天然記念物", MONUMENTS, kinds=("特別天然記念物",)),
    Dataset("natural-monuments", "天然記念物", MONUMENTS, kinds=("天然記念物",)),
    # 美術工芸品。建造物と同じ「国宝」でも、持つ項目が違うので分ける (ADR 0025)。
    Dataset(
        "national-treasures-of-fine-arts", "国宝（美術工芸品）", FINE_ARTS, kinds=("国宝",)
    ),
    Dataset(
        "important-cultural-properties-of-fine-arts", "重要文化財（美術工芸品）", FINE_ARTS
    ),
    Dataset(
        "registered-tangible-cultural-properties-of-fine-arts",
        "登録有形文化財（美術工芸品）",
        REGISTERED_FINE_ARTS,
    ),
    Dataset("registered-art-works", "登録美術品", REGISTERED_ART),
    # 民俗文化財
    Dataset(
        "important-tangible-folk-cultural-properties", "重要有形民俗文化財", TANGIBLE_FOLK
    ),
    Dataset(
        "registered-tangible-folk-cultural-properties",
        "登録有形民俗文化財",
        REGISTERED_TANGIBLE_FOLK,
    ),
    Dataset(
        "important-intangible-folk-cultural-properties", "重要無形民俗文化財", INTANGIBLE_FOLK
    ),
    Dataset(
        "registered-intangible-folk-cultural-properties",
        "登録無形民俗文化財",
        REGISTERED_INTANGIBLE_FOLK,
    ),
    Dataset(
        "documented-intangible-folk-cultural-properties",
        "記録作成等の措置を講ずべき無形の民俗文化財",
        DOCUMENTED_INTANGIBLE_FOLK,
    ),
    # 無形文化財
    Dataset("important-intangible-cultural-properties", "重要無形文化財", INTANGIBLE),
    Dataset(
        "registered-intangible-cultural-properties", "登録無形文化財", REGISTERED_INTANGIBLE
    ),
    Dataset(
        "documented-intangible-cultural-properties",
        "記録作成等の措置を講ずべき無形文化財",
        DOCUMENTED_INTANGIBLE,
    ),
    Dataset("selected-conservation-techniques", "選定保存技術", CONSERVATION_TECHNIQUES),
    # 記念物・文化的景観。内訳 (名勝地関係・遺跡関係…) では分けない (ADR 0025)。
    Dataset("registered-monuments", "登録記念物", REGISTERED_MONUMENTS),
    Dataset("important-cultural-landscapes", "重要文化的景観", CULTURAL_LANDSCAPES),
    # 世界遺産。他分類と行が重複する (ADR 0025)。
    Dataset("world-heritage-sites", "世界遺産", WORLD_HERITAGE),
)


KIND_SPLIT_CATEGORIES: Final = frozenset(
    dataset.category for dataset in TARGET_DATASETS if dataset.kinds
)
"""区分でリポジトリが分かれる分類。区分の欠けたレコードに気付くために使う。"""


def datasets_of(category: Category, kinds: Collection[str] = ()) -> list[Dataset]:
    """1 件をどのデータリポジトリへ書くか決める。定義順に返す。

    **複数返ることがある。** 401 には種別を 2 つ持つ複合指定があり、その 1 件は
    どちらの種別のリポジトリから見ても構成員なので、両方へ書く (ADR 0012)。
    区分に当てはまるものが無ければ受け皿へ落ち、受け皿も無ければ空になる。

    >>> [d.repo for d in datasets_of(DESIGNATED, ["国宝"])]
    ['national-treasures']
    >>> [d.repo for d in datasets_of(DESIGNATED, ["重要文化財"])]
    ['important-cultural-properties']
    >>> [d.repo for d in datasets_of(DESIGNATED)]
    ['important-cultural-properties']
    >>> [d.repo for d in datasets_of(MONUMENTS, ["特別名勝", "特別史跡"])]
    ['special-historic-sites', 'special-places-of-scenic-beauty']
    >>> datasets_of(MONUMENTS)
    []
    """
    wanted = set(kinds)
    of_category = [dataset for dataset in TARGET_DATASETS if dataset.category == category]
    if matched := [
        dataset for dataset in of_category if dataset.kinds and wanted.intersection(dataset.kinds)
    ]:
        return matched
    return [dataset for dataset in of_category if not dataset.kinds]


def datasets_for(categories: Sequence[Category]) -> list[Dataset]:
    """指定した分類が書きうるデータリポジトリを、定義順に返す。"""
    wanted = set(categories)
    return [dataset for dataset in TARGET_DATASETS if dataset.category in wanted]


@dataclass(frozen=True)
class Area:
    """検索の分割軸となる地域 (ADR 0002)。

    name は検索フォームの seat_pref にそのまま送る値で、コードではない。
    code と slug は送信には使わず、キャッシュのファイル名にだけ使う。
    """

    code: str
    """都道府県は JIS X 0401 のコード。都道府県でないものには 90 番台を割り当てた。"""

    name: str
    slug: str
    """ファイル名に使う ASCII 表記。

    日本語のファイル名は macOS が NFD で保持することがあり、リポジトリや
    別 OS との間で同じ名前が別物として扱われて再開判定がずれる。
    """


# 47 都道府県。並びと表記は seat_pref の select の option をそのまま写したもの。
PREFECTURES: Final[tuple[Area, ...]] = (
    Area("01", "北海道", "hokkaido"),
    Area("02", "青森県", "aomori"),
    Area("03", "岩手県", "iwate"),
    Area("04", "宮城県", "miyagi"),
    Area("05", "秋田県", "akita"),
    Area("06", "山形県", "yamagata"),
    Area("07", "福島県", "fukushima"),
    Area("08", "茨城県", "ibaraki"),
    Area("09", "栃木県", "tochigi"),
    Area("10", "群馬県", "gunma"),
    Area("11", "埼玉県", "saitama"),
    Area("12", "千葉県", "chiba"),
    Area("13", "東京都", "tokyo"),
    Area("14", "神奈川県", "kanagawa"),
    Area("15", "新潟県", "niigata"),
    Area("16", "富山県", "toyama"),
    Area("17", "石川県", "ishikawa"),
    Area("18", "福井県", "fukui"),
    Area("19", "山梨県", "yamanashi"),
    Area("20", "長野県", "nagano"),
    Area("21", "岐阜県", "gifu"),
    Area("22", "静岡県", "shizuoka"),
    Area("23", "愛知県", "aichi"),
    Area("24", "三重県", "mie"),
    Area("25", "滋賀県", "shiga"),
    Area("26", "京都府", "kyoto"),
    Area("27", "大阪府", "osaka"),
    Area("28", "兵庫県", "hyogo"),
    Area("29", "奈良県", "nara"),
    Area("30", "和歌山県", "wakayama"),
    Area("31", "鳥取県", "tottori"),
    Area("32", "島根県", "shimane"),
    Area("33", "岡山県", "okayama"),
    Area("34", "広島県", "hiroshima"),
    Area("35", "山口県", "yamaguchi"),
    Area("36", "徳島県", "tokushima"),
    Area("37", "香川県", "kagawa"),
    Area("38", "愛媛県", "ehime"),
    Area("39", "高知県", "kochi"),
    Area("40", "福岡県", "fukuoka"),
    Area("41", "佐賀県", "saga"),
    Area("42", "長崎県", "nagasaki"),
    Area("43", "熊本県", "kumamoto"),
    Area("44", "大分県", "oita"),
    Area("45", "宮崎県", "miyazaki"),
    Area("46", "鹿児島県", "kagoshima"),
    Area("47", "沖縄県", "okinawa"),
)

# 都道府県では引けない指定の受け皿。select の末尾 2 つで、２ は全角。
# 除くと複数県にまたがる指定などを取りこぼすため、分割軸に含める。
# 都道府県側と重複して現れても、統合時に (台帳ID, 管理対象ID) で排除できる。
NON_PREFECTURE_AREAS: Final[tuple[Area, ...]] = (
    Area("90", "２県以上", "multiple-prefectures"),
    Area("99", "地域を定めない", "unspecified"),
)

SELECTABLE_AREAS: Final[tuple[Area, ...]] = PREFECTURES + NON_PREFECTURE_AREAS
"""検索フォームの select が提供する 49 個。並びも表記もフォームどおり。"""

# データ側の都道府県が未正規化のまま入っている行の受け皿 (2026-08-11 に実測)。
# seat_pref は格納値の完全一致で絞るため、表示名でない値が入った行はどの option
# でも引けない。実害があったのは 101 の 3 件で、いずれも複数県にまたがる指定:
#   98 → わたらせ渓谷鐵道笠松トンネル (栃木・群馬) / 唐沢堰堤 (山梨・長野)
#   1  → 宮下家住宅主屋 (神奈川県)
# 元データが直れば 0 件になるだけで害はない。新しい値が現れたら、地域合計と
# 全国件数の差 (ledger.summarize) が気付かせてくれる。
IRREGULAR_AREAS: Final[tuple[Area, ...]] = (
    Area("91", "98", "raw-98"),
    Area("92", "1", "raw-1"),
)

SEARCH_AREAS: Final[tuple[Area, ...]] = SELECTABLE_AREAS + IRREGULAR_AREAS
"""``AreaScope.PREFECTURE`` の分割軸。既定の分類はこれで引く。"""

# 無形文化財・無形民俗文化財の select にだけ現れる 9 区分 (2026-08-23 実測)。
# 都道府県ではないので 80 番台を割り当てた。303 / 313 はこれしか持たない。
REGIONS: Final[tuple[Area, ...]] = (
    Area("80", "全国一円", "nationwide"),
    Area("81", "東北", "tohoku"),
    Area("82", "関東", "kanto"),
    Area("83", "北陸", "hokuriku"),
    Area("84", "東海", "tokai"),
    Area("85", "近畿", "kinki"),
    Area("86", "中国", "chugoku"),
    Area("87", "四国", "shikoku"),
    Area("88", "九州", "kyushu"),
)

WHOLE_AREA: Final = Area("00", "", "whole")
"""地域で絞らない 1 回ぶん。``seat_pref`` に空を送ると全国が返る。

選定保存技術 (304) は検索フォームに地域欄そのものが無く、CSV にも地域の列が
無い (2026-08-23 実測。全 82 件)。分けようがないので 1 回で取る。
"""

_AREAS_BY_SCOPE: Final[dict[AreaScope, tuple[Area, ...]]] = {
    AreaScope.PREFECTURE: SEARCH_AREAS,
    AreaScope.PREFECTURE_AND_REGION: SEARCH_AREAS + REGIONS,
    AreaScope.REGION: REGIONS,
    AreaScope.WHOLE: (WHOLE_AREA,),
}


def search_areas(category: Category) -> tuple[Area, ...]:
    """その分類を取るときの分割軸 (2026-08-23 実測)。

    **分類によって引ける単位が違う。** 都道府県で引けない分類があり、
    地域欄そのものが無い分類もある。全分類に同じ 51 地域を投げると、
    引けない分類では 0 件の検索を 51 回繰り返すことになる。

    >>> len(search_areas(REGISTERED))
    51
    >>> [area.name for area in search_areas(Category("304", "選定保存技術", 82,
    ...     area_scope=AreaScope.WHOLE))]
    ['']
    """
    return _AREAS_BY_SCOPE[category.area_scope]


def areas_for(category: Category, areas: Sequence[Area] | None) -> Sequence[Area]:
    """明示された地域があればそれ、無ければその分類の分割軸。

    取得も報告も出力も「分類 × 地域」で回るので、**地域は分類ごとに決まる**
    必要がある。呼び手が ``--area`` で絞ったときだけ、そちらを優先する。

    >>> len(areas_for(REGISTERED, None))
    51
    >>> [area.name for area in areas_for(REGISTERED, [PREFECTURES[12]])]
    ['東京都']
    """
    return search_areas(category) if areas is None else areas


def all_search_areas(categories: Sequence[Category]) -> tuple[Area, ...]:
    """引き渡す分類が使う地域をまとめたもの。定義順で重複を落とす。

    CLI の ``--area`` の選択肢や、複数分類をまとめて扱う場面で使う。
    """
    found: dict[str, Area] = {}
    for category in categories:
        for area in search_areas(category):
            found.setdefault(area.code, area)
    return tuple(found.values())


def detail_url(category_code: str, kanri_taishou_id: str) -> str:
    """詳細ページの URL を組み立てる。

    **第 1 セグメントは分類コードで、CSV の台帳ID ではない** (2026-08-23 実測)。
    現行 4 分類は台帳ID と分類コードが同値なので取り違えても表に出ないが、
    登録記念物 (411) の台帳ID は 401 で、401 で組むと「必要な情報が足りません」の
    エラーページが返る。台帳ID が分類コードと食い違うのは 211 / 311 / 322 /
    312 / 323 / 313 / 411 / 412 の 8 分類。

    ID は必ず文字列のまま扱うこと。管理対象ID には短い連番形式 (``23``) と
    8 桁ゼロ詰め形式 (``00003904``) が混在し、数値に変換するとゼロ詰めが落ちて
    到達できなくなる。

    >>> detail_url("102", "23")
    'https://kunishitei.bunka.go.jp/heritage/detail/102/23'
    >>> detail_url("102", "00003904")
    'https://kunishitei.bunka.go.jp/heritage/detail/102/00003904'
    >>> detail_url("411", "00003483")  # 台帳ID は 401
    'https://kunishitei.bunka.go.jp/heritage/detail/411/00003483'
    """
    if not category_code or not kanri_taishou_id:
        raise ValueError("分類コードと 管理対象ID は必須")
    return f"{BASE_URL}/heritage/detail/{category_code}/{kanri_taishou_id}"
