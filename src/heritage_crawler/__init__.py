"""国指定文化財等データベースから建造物関連データを抽出するクローラー。"""

from heritage_crawler.catalog import (
    IRREGULAR_AREAS,
    NON_PREFECTURE_AREAS,
    PREFECTURES,
    SEARCH_AREAS,
    SELECTABLE_AREAS,
    TARGET_CATEGORIES,
    Area,
    Category,
    detail_url,
)

__all__ = [
    "IRREGULAR_AREAS",
    "NON_PREFECTURE_AREAS",
    "PREFECTURES",
    "SEARCH_AREAS",
    "SELECTABLE_AREAS",
    "TARGET_CATEGORIES",
    "Area",
    "Category",
    "detail_url",
]
