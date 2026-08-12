# 0013: 対応するデータが存在しないリポジトリは削除する

## 状態

承認済み (2026-08-12)。
[0009](0009-output-to-existing-per-type-repositories.md) の「取得対象でない
リポジトリは空のまま残す」を置換する。既存リポジトリを使い新しくは作らない点、
出力の単位をデータセットとする点は変わらない。

## 文脈

[0009](0009-output-to-existing-per-type-repositories.md) の時点でデータリポジトリは
11 個あり、埋まるのは 4 個だった。残る 7 個は「中身を持てる段階になってから
整える」として空のまま残していた。

[0012](0012-crawl-monuments-and-route-by-kind.md) で記念物 (401) を取得対象に
加えた結果、7 個のうち 6 個が埋まった。残る 1 個
`preservation-districts-for-groups-of-traditional-buildings` だけは、**制度上
データが存在しない**。

- 国が選定するのは**重要**伝統的建造物群保存地区 (分類コード 103) であり、
  それは `important-preservation-districts-for-groups-of-traditional-buildings`
  に入っている
- 伝統的建造物群保存地区そのものの決定は市町村が行うため、国指定文化財等
  データベースの対象ではない

つまりこのリポジトリは、クローラーを直しても、取得対象を広げても埋まらない。
空のまま置くと「これから埋まる」という誤ったシグナルを出し続け、10 個が
埋まっているのに 11 個中 10 個という説明を毎回することになる。

## 決定

- **`preservation-districts-for-groups-of-traditional-buildings` を削除する。**
  データリポジトリは 10 個とする
- **削除は人が手で行う。** クローラーもエージェントも、リポジトリの削除のような
  取り消せない操作は実行しない
- 制度が変わって国がこの区分のデータを持つようになったら、そのときに作り直す。
  リポジトリ名は決め直せるので、先に空の器を用意しておく利点は無い
- 今後も同じ判断をする — **データが存在しないと分かった器は残さない。** ただし
  「今は取得していないが、取得すれば埋まる」ものは対象外 (それは残す)

## 影響

- データリポジトリは 11 個から **10 個**になる。`code4heritage` org 配下で
  クローラーが書くのは
  [0009](0009-output-to-existing-per-type-repositories.md) と
  [0012](0012-crawl-monuments-and-route-by-kind.md) の対応表にある 10 個ちょうど
- README と CLAUDE.md から「空のまま残る 1 個」の説明が消える。代わりに、
  重要伝統的建造物群保存地区と伝統的建造物群保存地区の違いは
  `important-preservation-districts-for-groups-of-traditional-buildings` の
  README で説明する (取り違えは利用者側でも起きるため)
- `src/heritage_crawler/catalog.py` の `TARGET_DATASETS` は変わらない
  (もともとこのリポジトリを書き先に持っていない)
