# テスト用の fixture

国指定文化財等データベースの**実応答から必要な箇所だけを切り出した** HTML。
解析 (`src/heritage_crawler/detail_page.py` / `src/heritage_crawler/search_page.py` /
`src/heritage_crawler/listing.py`) のテストが読む。

実物を置いているのは、**マークアップの癖こそが解析の相手**だから。属性の並び・タブ・
数値文字参照・見出しに入れ子のフォーム・ページャが 2 度出ること — 手で書き起こすと
これらが落ちて、テストが通るのに本番で読めないものができる。

## 出典

```
出典：「国指定文化財等データベース」（文化庁）
（https://kunishitei.bunka.go.jp/）（2026年8月11日および2026年8月12日に利用）
上記を加工して作成
```

文字情報は出典を記載すれば自由に利用できる
([利用規約](https://kunishitei.bunka.go.jp/top/policy) /
[ADR 0007](../../docs/decisions/0007-redistribute-text-with-attribution.md))。
このリポジトリが出力するデータと同じ扱いで、fixture にも出典を付す。

**ここには利用日を書く。** ADR 0007 が「利用日を散文に書き込まない」と言うのは、
データが更新されるたびに動く値のことで、正本は各データリポジトリの `meta.json` に
ある。fixture の取得日は**切り出した時点で固定**され、二度と動かない。

| 取得日 | ファイル |
|---|---|
| 2026-08-11 | `search_index.html` / `search_hit.html` / `search_empty.html` |
| 2026-08-12 | `detail_101.html` / `detail_102.html` / `detail_103.html` / `detail_401.html` / `listing_p1.html` / `listing_p2.html` |

## 画像は含まない

**画像の実体は 1 バイトも入っていない。** 画像は作品毎に個別の許諾が必要なので、
このクローラーは取得も再配布もしない (ADR 0007)。

`detail_103.html` には写真の URL が `<img src="...">` として残っているが、これは
**詳細ページのマークアップの一部**であって画像そのものではない。解析が「写真の有無」
だけをメタデータとして読むので、そこを削ると検査の対象が消える。

## 加工した点

- **必要な箇所だけを切り出してある。** 共通のヘッダ・フッタ・スクリプトは落とした
- **`_csrfToken` を含むものはダミー値 (`dddd...`) に差し替えてある**
  (`search_hit.html` / `search_index.html` / `listing_p1.html` / `listing_p2.html`)。
  実際のセッションの値をリポジトリに残さないため
- `listing_p1.html` は行を 5 件中 3 件に減らしてある

どのファイルが何を代表しているか (どの分類の・どんな特徴を持つページか) は、
各ファイルの冒頭コメントに書いてある。
