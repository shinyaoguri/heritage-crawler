# 0012: 記念物 (401) を取得対象に加え、種別でリポジトリへ振り分ける

## 状態

承認済み (2026-08-12)。
[0009](0009-output-to-existing-per-type-repositories.md) の対応表を拡張する
(既存リポジトリを使う・出力の単位はデータセット・ファイル配置は変えない)。

## 文脈

[0009](0009-output-to-existing-per-type-repositories.md) の時点で、`code4heritage`
配下のデータリポジトリ 11 個のうち埋まるのは建造物系の 4 個だけだった。残る 7 個を
埋めるべく、2026-08-12 に検索フォームと台帳・詳細ページを実地で調べた。

- **6 個は分類コード 401 (史跡名勝天然記念物) 1 つに同居している。** 検索フォームの
  `register_sub_id` に史跡・名勝・天然記念物の別は無く、401 を引いてから種別で
  分けることになる。全国 **3,281 件** (指定単位)
- **401 は棟に展開されない** (実測した 513 行すべて棟名が空)。101 と同じく指定 = 1 行
- CSV の列構成は建造物系と同じ 18 列で、種別は `種別1` / `種別2` に入る
- **種別を 2 つ持つ複合指定がある** — 旧浜離宮庭園 (特別名勝 + 特別史跡)、
  浦富海岸 (名勝 + 天然記念物) など。実測 513 行のうち 36 行
- 詳細ページで [0008](0008-normalize-schema-detail-page-wins.md) の対応表に無い
  ラベルは 5 つ (`指定年月日` / `特別指定年月日` / `特別区分` / `指定基準` /
  `所在地（市区町村）`)。`指定基準` だけは 1 欄に**カンマ区切り**で複数入る
- **`detail_rellist_*` モーダルの中身が分類で変わる。** 建造物系は附指定の一覧
  (`附名称` / `附員数`)、401 は指定等後に行った措置の履歴
  (`異動年月日` / `異動種別1`〜`3` / `異動内容`)
- 残る 1 個 `preservation-districts-for-groups-of-traditional-buildings` に
  対応するデータは**存在しない**。国が選定するのは重要伝統的建造物群保存地区で、
  伝統的建造物群保存地区の決定は市町村が行うためデータベースの対象外
  ([0009](0009-output-to-existing-per-type-repositories.md) の再確認)

振り分けで迷うのは複合指定と特別指定の 2 つで、どちらもデータの正しさではなく
**リポジトリを何の一覧として読ませたいか**で決まる。

## 決定

- **401 を取得対象に加える。** 分類の定義は `src/heritage_crawler/catalog.py` の
  `TARGET_CATEGORIES`、出力先の対応表は同じく `TARGET_DATASETS` を正本とする
  (`BUILDING_*` から改名した — 401 は建造物ではない)

  | リポジトリ | 受ける種別 |
  |---|---|
  | `special-historic-sites` | 特別史跡 |
  | `historic-sites` | 史跡 |
  | `special-places-of-scenic-beauty` | 特別名勝 |
  | `places-of-scenic-beauty` | 名勝 |
  | `special-natural-monuments` | 特別天然記念物 |
  | `natural-monuments` | 天然記念物 |

- **複合指定は、種別の数だけ複数のリポジトリへ書く。** 旧浜離宮庭園は
  `special-places-of-scenic-beauty` と `special-historic-sites` の両方に同じ行が
  載る。各リポジトリを「その種別の完全な一覧」として読めることを、行の重複より
  優先する。重複は `(台帳ID, 管理対象ID)` と `url` が同一なので後から名寄せできる
- **特別◯◯ と ◯◯ は排他に振り分ける。** 特別史跡は史跡のうちから指定されるが、
  `historic-sites` には書かない。国宝を `national-treasures` へ排他に送る
  [0009](0009-output-to-existing-per-type-repositories.md) と揃える
- **401 に受け皿のリポジトリを置かない。** 区分が読めなかったものを最多の史跡へ
  流すと、振り分けの誤りが史跡の中に紛れて見えなくなる。どこへも書かず
  `build-records` の報告に名前を出す (102 は従来どおり重要文化財側が受け皿)
- **1 欄にカンマで詰められた値は割って配列にする** (`指定基準`)。空要素は落とす。
  基準の文言そのものの区切りは読点なので割られない
- **`detail_rellist_*` の解釈はラベルで見分ける。** 分類で決め打ちにしない。
  401 の措置の履歴は `measures` として残す (`日付` / `種別` / `内容`)。読み取り
  (`detail_page`) は中身に意味を与えず、対応表は `record` に置く
  ([0008](0008-normalize-schema-detail-page-wins.md) の分離をそのまま守る)
- `preservation-districts-for-groups-of-traditional-buildings` は**空のまま残す**。
  データベースに対応するデータが無い以上、クローラー側でできることは無い

## 影響

- 取得対象は棟単位 20,461 件から **約 23,700 件**になる (401 が約 3,280 件)。
  初回の取得は 1 req/s で約 1 時間の追加
- 出力先のデータリポジトリは 4 個から **10 個**になる。月次更新
  ([#10](https://github.com/shinyaoguri/heritage-crawler/issues/10)) の push 先も
  同じだけ増える
- 6 リポジトリの行数の合計は 401 の指定件数を**上回る** (複合指定のぶん)。
  件数を突き合わせるときは、合計ではなく `(台帳ID, 管理対象ID)` の異なり数を見る
- スキーマに `special_class` / `special_designated_date` / `measures` /
  `has_measures` が加わる。建造物系の行には現れない (値が空ならキーごと落とす)
- `Dataset.national_treasure_class` は `kinds` に一般化された。102 の振り分け結果は
  変わらない
- [0002](0002-two-stage-fetch-csv-then-detail.md) の 2 段構えと分割軸 (都道府県)、
  [0011](0011-back-off-to-1-rps-and-detect-error-pages.md) のレート上限は変えない
