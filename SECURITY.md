# セキュリティポリシー

## 報告の窓口

このリポジトリのコードやワークフローに脆弱性を見つけたら、公開の Issue ではなく
**GitHub の非公開の脆弱性報告**を使ってください。リポジトリの **Security** タブ →
**Report a vulnerability** から送れます。報告の内容は、修正が済むまで公開されません。

とくに気にしているのは次のものです。

- `.github/workflows/` のワークフローを経由して、データリポジトリへの push に使う
  GitHub App の資格情報 (`DATA_PUSH_CLIENT_ID` / `DATA_PUSH_PRIVATE_KEY`) に届く経路
- クローラーが、取得先 (国指定文化財等データベース) へ上限 (1 req/s) を超えて
  リクエストを送ってしまう不具合

## 対象外

- **収録データの誤り** — データリポジトリか
  [code4heritage/heritages](https://github.com/code4heritage/heritages) の Issue へ。
  元のデータベースの誤りは、そのまま写しているだけのこともあります
- **国指定文化財等データベースそのもの** — 運営元の文化庁へ。このリポジトリは
  運営元とは関係ありません
