"""国指定文化財等データベースから建造物関連データを抽出するクローラー。"""

from heritage_crawler.catalog import (
    BUILDING_CATEGORIES,
    Category,
    detail_url,
)

__all__ = ["BUILDING_CATEGORIES", "Category", "detail_url"]
