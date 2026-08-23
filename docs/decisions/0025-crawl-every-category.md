# 0025: 全 19 分類を取得し、種別ごとのリポジトリへ書く

## 状態

承認済み (2026-08-23)。
[0009](0009-output-to-existing-per-type-repositories.md) /
[0012](0012-crawl-monuments-and-route-by-kind.md) の対応表を**拡張する** (置換ではない)。
既存 10 リポジトリの受け持ちも、種別で分ける方針も変えない。

## 文脈

取得対象は 101 / 102 / 103 / 401 の 4 分類 (23,742 件) だった。検索フォームの
`register_sub_id` が提供するのは **19 分類**で、残る 15 分類 (12,870 件) —
美術工芸品・民俗文化財・無形文化財・選定保存技術・登録記念物・重要文化的景観・
世界遺産 — は手つかずだった。データベースが持つ全データを網羅したい。

2026-08-23 に 15 分類すべてで CSV も詳細ページも取れることを実地で確かめた
([#74](https://github.com/shinyaoguri/heritage-crawler/issues/74))。あわせて、
現行 4 分類でだけ成り立っていた 4 つの前提を外してある
([#75](https://github.com/shinyaoguri/heritage-crawler/pull/75) /
[#76](https://github.com/shinyaoguri/heritage-crawler/pull/76) /
[#77](https://github.com/shinyaoguri/heritage-crawler/pull/77) /
[#78](https://github.com/shinyaoguri/heritage-crawler/pull/78))。

分け方で決めることは 3 つあった。

### 1. 美術工芸品の「国宝」を既存の `national-treasures` へ入れるか

制度上の種別で見れば、建造物の国宝も美術工芸品の国宝も同じ「国宝」。
既存リポジトリへ合流させれば `national-treasures` が「国宝の完全な一覧」になる。

一方で持つ項目がまるで違う。建造物は `構造及び形式等` / `創建及び沿革`、
美術工芸品は `員数` / `作者` / `国` / `ト書` / `寸法・重量`。件数も桁が違う
(国宝は建造物 約 290 に対し美術工芸品 約 900)。

### 2. 1 分類の中の内訳で分けるか

401 は史跡・名勝・天然記念物とその特別指定の 6 リポジトリに分けている
([0012](0012-crawl-monuments-and-route-by-kind.md))。同じことを登録記念物
(名勝地関係 129 / 遺跡関係 13 / 動物植物地質鉱物関係 6) や重要無形民俗文化財
(民俗芸能 / 風俗慣習 / 民俗技術) にも当てはめるか。

### 3. 世界遺産を含めるか

世界遺産は文化財保護法の指定・登録・選定ではなく、**既指定の文化財を束ねたもの**。
`catalog.py` は「建造物・記念物と別軸の指定」として明示的に除外していた。

## 決定

- **全 19 分類を取得対象にする。** `TARGET_CATEGORIES` が正本
- **データリポジトリは 26 個。** 新規 16 個を `code4heritage` 配下に作る。
  `TARGET_DATASETS` が正本

  | リポジトリ | 分類 |
  |---|---|
  | `national-treasures-of-fine-arts` | 201 のうち国宝 |
  | `important-cultural-properties-of-fine-arts` | 201 のうち重要文化財 |
  | `registered-tangible-cultural-properties-of-fine-arts` | 211 |
  | `registered-art-works` | 202 |
  | `important-tangible-folk-cultural-properties` | 301 |
  | `registered-tangible-folk-cultural-properties` | 311 |
  | `important-intangible-folk-cultural-properties` | 302 |
  | `registered-intangible-folk-cultural-properties` | 322 |
  | `documented-intangible-folk-cultural-properties` | 312 |
  | `important-intangible-cultural-properties` | 303 |
  | `registered-intangible-cultural-properties` | 323 |
  | `documented-intangible-cultural-properties` | 313 |
  | `selected-conservation-techniques` | 304 |
  | `registered-monuments` | 411 |
  | `important-cultural-landscapes` | 412 |
  | `world-heritage-sites` | 901 |

- **美術工芸品は既存リポジトリへ合流させず、専用のリポジトリを作る** (論点 1)。
  スキーマ差を分離できることを、「国宝」を 1 箇所で引けることより優先する。
  既存の `national-treasures` / `important-cultural-properties` /
  `registered-tangible-cultural-properties` は建造物のまま意味を変えない
- **1 分類の中の内訳では分けない** (論点 2)。登録記念物は 1 個、重要無形民俗文化財も
  1 個。**401 を分けたのは、史跡・名勝・天然記念物が制度上それぞれ別の種別**
  だからで、名勝地関係 / 遺跡関係 は登録記念物という 1 種別の内訳にすぎない。
  内訳はレコードの `types` に載るので、絞り込みは閲覧サイト
  ([0015](0015-single-cross-type-site-on-pages.md)) に任せる
- **世界遺産を含める** (論点 3)。ただし**他分類と別軸**であることを明記する —
  `world-heritage-sites` の 20 件は、構成資産として他リポジトリにも現れる。

  **キーは重ならない** (2026-08-23 の全件取得で判明)。取得前は 401 の複合指定
  ([0012](0012-crawl-monuments-and-route-by-kind.md)) と同じく
  `(台帳ID, 管理対象ID)` で名寄せできると見ていたが、**実際には 1 キーも共有して
  いない**。401 の複合指定は同じ 1 件が 2 つの種別から指定されたものなので同じキーを
  持つのに対し、901 は**別の台帳に立った別のレコード**で、構成資産を
  `二荒山神社、東照宮、輪王寺` のような 1 本の文字列で参照しているだけだった。

  手掛かりは名称しかなく、突き合わせても**構成資産 244 個のうち当たるのは 62 個**。
  当たる側も危うい — 「東照宮」で引くと日光の 32 棟に加えて和歌山・埼玉・宮城・
  愛知・群馬・京都の 28 棟が当たる。**閲覧サイトは名寄せしない**
  ([heritages#28](https://github.com/code4heritage/heritages/issues/28)) —
  役目は原本を素直に見せることで、推測で結び付けることではない
- **振り分けの区分は 201 も `国宝・重文区分`** (`record.ROUTING_KEYS`)。
  102 と同じキーで、詳細ページに必ずある (2026-08-23 実測)
- **`Category` が台帳ID を持つ。** 台帳ID には複数の分類が同居するので (#74)、
  一覧から台帳の行を組み立て直すとき ([0017](0017-audit-completeness-with-the-search-listing.md))
  に分類側から採る

## 影響

- 取得対象は 23,742 件 → **約 36,600 件**。初回の全件取得は 1 req/s で約 4 時間の追加
- データリポジトリは 10 個 → **26 個**。週次 ([0020](0020-check-weekly-by-diffing-the-ledger-csv.md))
  の push 先も同じだけ増える。台帳 CSV の artifact も 189 → 約 400 ファイル
- 詳細ページの項目が増える。原文ラベル 22 個と新しいキー 10 個
  ([0008](0008-normalize-schema-detail-page-wins.md) の対応表を拡張)
- `designation_kind` に**選択** (312 / 313) と**認定** (303 / 323 / 304) が加わる。
  制度上それぞれ別の行為なので、まとめて「指定」とは呼ばない
- **緯度経度を持たない分類がある** (303 / 313 / 323 / 304)。地図
  ([0016](0016-gsi-tiles-and-vendored-map-library.md)) には出ないので、
  閲覧サイトは 0 件の地図を描かない配慮が要る
- [0013](0013-delete-the-repository-without-data.md) の「データが存在しないと
  分かった器は残さない」は守る。16 分類すべてに 1 件以上あることを確認済み
