# 0024: レコードは由来の分類コードを自分で持つ

## 状態

承認済み (2026-08-23)。[0008](0008-normalize-schema-detail-page-wins.md) のスキーマに
キーを 1 つ足す。原文ラベルの寄せ方・日付・欠損の扱いは変えない。

## 文脈

[0009](0009-output-to-existing-per-type-repositories.md) は「レコードの `ledger_id` と
`category_name` は残す。リポジトリが分かれても 1 行から由来の分類が読める」と
決めた。実装はこれを**台帳ID から分類コードを引く**形で満たしていた
(`export.build_dataset` / `update.read_existing` / `update.candidates`)。

```python
category = CATEGORIES_BY_CODE[str(record["ledger_id"])]
```

取得対象の 4 分類 (101 / 102 / 103 / 401) では台帳ID と分類コードが同値なので、
これで正しく戻せていた。**全 19 分類ではそうならない** (2026-08-23 実測。
[#74](https://github.com/shinyaoguri/heritage-crawler/issues/74))。

| 台帳ID | 同居する分類 |
|---|---|
| 201 | 201 国宝・重要文化財（美術工芸品） / 211 登録有形文化財（美術工芸品） |
| 301 | 301 重要有形民俗文化財 / 311 登録有形民俗文化財 |
| 302 | 302 重要無形民俗文化財 / 322 登録無形民俗文化財 / 312 記録作成等の措置を講ずべき無形の民俗文化財 |
| 303 | 303 重要無形文化財 / 323 登録無形文化財 / 313 記録作成等の措置を講ずべき無形文化財 |
| 401 | 401 史跡名勝天然記念物 / 411 登録記念物 / 412 重要文化的景観 |

台帳ID で引くと、登録記念物の行が史跡名勝天然記念物として戻る。**差分更新では
これが行の消失につながる** — 分類は「消してよいか」(その分類の網羅性が
確かめられているか) と「どのリポジトリへ書くか」の両方を決めるため。

戻す手は 3 つあった。

- **`category_name` から引く。** 既存の出力が 1 バイトも変わらない。ただし
  `category_name` は原文の分類名で、相手先の表記が変われば逆引き表が壊れる。
  [0008](0008-normalize-schema-detail-page-wins.md) が読み取りとスキーマを分けた
  意図 (表記が変わっても直す場所は 1 つ) に反する
- **ファイルの置き場から引く。** リポジトリと分類は 1:1 でないので戻せない
  (`important-cultural-properties` には 102 が、`historic-sites` には 401 が入るが、
  分類とリポジトリの対応は多対多。[0012](0012-crawl-monuments-and-route-by-kind.md))
- **レコードが自分で名乗る。** キーが 1 つ増える

## 決定

- **レコードに `category_code` を持たせる。** 値は取得元の分類コード
  (`Category.code` = 検索フォームの `register_sub_id`)。`ledger_id` は CSV の
  台帳ID 列の値のまま残す — 原文の識別子として意味があり、`managed_id` と
  組んだキーは分類をまたいでも一意
- **分類を戻すのは `record.category_of` に集約する。** 呼び出し側 (`export` /
  `update`) は台帳ID を見ない
- **`category_code` を持たない行は台帳ID から戻す。** 2026-08-23 より前に書いた
  行への控えで、当時の 4 分類では両者が同値なのでこれで正しい。全件を
  組み立て直せば消えるが、**差分更新の 1 回目が前回の出力を読めなくなるのを防ぐ**
  ために残す
- 表示名は `分類コード` (`record.DERIVED_LABELS`)。キーの並びでは `managed_id` と
  `category_name` の間に置く ([0014](0014-machine-readable-dataset-metadata.md) の
  `meta.json` にも自動で載る)

## 影響

- **既存 10 データリポジトリの全 415 ファイルに 1 キー増える。** 全件を
  組み立て直して push する必要がある (23,742 行 × 約 26 バイト)
- [0008](0008-normalize-schema-detail-page-wins.md) の `KEY_ORDER` が 1 つ伸びる。
  読む側は増えたキーを無視してよい (既存キーの意味も並びも変わらない)
- 閲覧サイト ([0015](0015-single-cross-type-site-on-pages.md)) は `meta.json` の
  表示名を読むので、手を入れずに新しいキーが出る
- 分類が増えても、消してよいかの判定 ([0021](0021-record-removals-with-evidence.md))
  と書き先の決定 ([0012](0012-crawl-monuments-and-route-by-kind.md)) がそのまま働く
