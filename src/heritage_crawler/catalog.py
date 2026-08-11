"""データベースの分類コードと、詳細ページ URL の組み立て。

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


# 建造物に関連する分類。世界遺産 (901) は建造物と別軸の指定のため含めない。
BUILDING_CATEGORIES: Final[tuple[Category, ...]] = (
    Category("101", "登録有形文化財（建造物）"),
    Category("102", "国宝・重要文化財（建造物）"),
    Category("103", "重要伝統的建造物群保存地区"),
)


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
