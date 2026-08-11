"""データベースの語彙 (分類・検索の地域) と、詳細ページ URL の組み立て。

ここにある値と URL 形式は 2026-08-11 に実データベースへアクセスして確認したもの
(ADR 0002 と Issue #1 の調査所見コメントを参照)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

BASE_URL: Final = "https://kunishitei.bunka.go.jp"


@dataclass(frozen=True)
class Category:
    """文化財分類。code は検索フォームの register_sub_id かつ CSV の台帳ID。"""

    code: str
    name: str
    known_designation_count: int
    """2026-08-11 に実測した指定件数。

    棟ではなく**指定**の件数で、検索結果の件数表示と同じ単位。新規指定・解除で
    増減するため厳密一致の検査には使えないが、取得の欠損を見つける目安になる。
    """


# 建造物に関連する分類。世界遺産 (901) は建造物と別軸の指定のため含めない。
BUILDING_CATEGORIES: Final[tuple[Category, ...]] = (
    Category("101", "登録有形文化財（建造物）", 14748),
    Category("102", "国宝・重要文化財（建造物）", 2633),
    Category("103", "重要伝統的建造物群保存地区", 126),
)


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


def detail_url(daichou_id: str, kanri_taishou_id: str) -> str:
    """CSV の 2 列から詳細ページの URL を組み立てる。

    ID は必ず文字列のまま扱うこと。管理対象ID には短い連番形式 (``23``) と
    8 桁ゼロ詰め形式 (``00003904``) が混在し、数値に変換するとゼロ詰めが落ちて
    到達できなくなる。

    >>> detail_url("102", "23")
    'https://kunishitei.bunka.go.jp/heritage/detail/102/23'
    >>> detail_url("102", "00003904")
    'https://kunishitei.bunka.go.jp/heritage/detail/102/00003904'
    """
    if not daichou_id or not kanri_taishou_id:
        raise ValueError("台帳ID と 管理対象ID は必須")
    return f"{BASE_URL}/heritage/detail/{daichou_id}/{kanri_taishou_id}"
