# 0009: データは種別ごとの既存リポジトリへ出力する

## 状態

承認済み (2026-08-12)。
[0001](0001-split-repositories-by-heritage-type.md) のデータリポジトリの粒度と
管理主体、[0004](0004-output-jsonl-per-prefecture.md) のファイル配置を置換する。

## 文脈

[0001](0001-split-repositories-by-heritage-type.md) は、データリポジトリを
**建造物系 (101 / 102 / 103) をまとめた 1 リポジトリ**とし、個人アカウント
(shinyaoguri) で管理すると決めていた。org を離れた理由は、Free プランでは
private リポジトリの ruleset (main 保護・PR 必須) と auto-merge が使えない点にある。

2026-08-12 に出力先の作成に着手したところ、**データリポジトリは既に存在していた**。
`code4heritage` org 配下に 2026-07-23 作成の 11 リポジトリがあり、いずれも private で
中身は README のタイトル 1 行だけだった。[0001](0001-split-repositories-by-heritage-type.md)
の「空のリポジトリを先に作らない」は、この ADR より前に破られていたことになる。

11 リポジトリは**文化財の種別ごと**に分かれている。建造物系の取得対象
(分類コード 101 / 102 / 103) に対応するのは 4 つ。

| リポジトリ | 対応する取得対象 |
|---|---|
| `registered-tangible-cultural-properties` | 101 登録有形文化財（建造物） |
| `national-treasures` | 102 のうち国宝 |
| `important-cultural-properties` | 102 のうち重要文化財 |
| `important-preservation-districts-for-groups-of-traditional-buildings` | 103 重要伝統的建造物群保存地区 |

- **分類コードとリポジトリは 1:1 でない。** 102 (国宝・重要文化財) が 2 リポジトリに
  分かれている。振り分けには詳細ページの「国宝・重文区分」が使える
  (実データ 30 件すべてに値があった。国宝 4 / 重要文化財 26)
- 残る 7 リポジトリ (史跡・特別史跡・名勝・特別名勝・天然記念物・特別天然記念物・
  伝統的建造物群保存地区) は今回の取得対象ではない。うち
  `preservation-districts-for-groups-of-traditional-buildings` に対応するデータは
  そもそも無い — 国が選定するのは**重要**伝統的建造物群保存地区であり、
  伝統的建造物群保存地区の決定は市町村が行うためデータベースの対象外

既存の 11 リポジトリを捨てて 1 リポジトリを作り直すこともできたが、種別ごとに
公開・共有を判断できる粒度は [0001](0001-split-repositories-by-heritage-type.md) の
狙い (種別ごとに独立した履歴を持つ) に沿っており、既存の設計を活かす方を選んだ。

## 決定

- **出力先は種別ごとのリポジトリとし、`code4heritage` 配下の既存リポジトリを使う。**
  新しいデータリポジトリは作らない
- クローラーは**データリポジトリ 1 つを出力の単位 (データセット) として持つ**。
  定義は `src/heritage_crawler/catalog.py` の `BUILDING_DATASETS` を正本とする
- ファイル配置は次のとおり。分類コードの階層は落とす (リポジトリが種別 1 つに
  対応するので冗長になる)

  ```
  <出力ディレクトリ>/<リポジトリ名>/data/<都道府県コード>_<ローマ字表記>.jsonl
  例: national-treasures/data/29_nara.jsonl
  ```

  `--output-dir` はデータリポジトリを並べた**親ディレクトリ**を指す
  (ローカルでは `~/Repos/bunkazai`)
- **102 の振り分けは詳細ページの「国宝・重文区分」で決める。** 国宝 →
  `national-treasures`、それ以外 → `important-cultural-properties`。国宝は
  重要文化財のうちから指定されるが、リポジトリには排他に振り分ける。区分が
  欠けた棟は重要文化財側へ送り、件数を報告に出す (取りこぼしに気付けるように)
- **org の Free プランの制約を受け入れる。** private リポジトリでは ruleset と
  auto-merge が使えないため、データリポジトリでは main 保護を効かせない。
  public 化した時点で入れられる
- 可視性は private のまま置く。public 化は内容が公開に耐える状態になった時点で
  判断する ([0007](0007-redistribute-text-with-attribution.md))

## 影響

- [0001](0001-split-repositories-by-heritage-type.md) のうち、データリポジトリを
  建造物系 1 つにまとめる点と個人アカウントで管理する点は置換される。
  クローラー本体 (`heritage-crawler`) は個人アカウントのまま変えない
- [0004](0004-output-jsonl-per-prefecture.md) のファイル配置
  (`data/<分類コード>/<都道府県>.jsonl`) は置換される。JSON Lines で 1 行 1 棟、
  キー `(台帳ID, 管理対象ID)` で安定ソートする点は変わらない
- レコードの `ledger_id` と `category_name` は残す。リポジトリが分かれても
  1 行から由来の分類が読める ([0008](0008-normalize-schema-detail-page-wins.md))
- 月次更新 ([#10](https://github.com/shinyaoguri/heritage-crawler/issues/10)) の
  push 先は 4 リポジトリになる。差分が無いリポジトリには何もコミットしない
- 各データリポジトリに LICENSE と README を入れる
  ([0007](0007-redistribute-text-with-attribution.md) の書式)。取得対象でない
  7 リポジトリは空のまま残す — 中身を持てる段階になってから整える
