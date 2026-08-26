# demo_sales — デモ用売上データベース

NL→SQL パイプライン検証用の、架空の小売チェーンの売上データ。既存 DB `llm_pipe_lab`
（Postgres, localhost:5432）の中に **`demo_sales` スキーマ**として構築する。
アプリ用テーブル（`public` の `nl2sql_experiment` 等）とは分離している。

- 金額はすべて **税抜・円（整数）**。
- 「直近2年」の基準日は再現性のため **2026-08-14 に固定**（対象期間 2024-08-15〜2026-08-14）。
- 乱数シード固定（既定 `20260814`）。同じシードなら何度実行しても同一データ。

## ファイル構成

| ファイル | 実行ユーザー | 役割 |
|---|---|---|
| `001_schema.sql` | `ppln_dev_user` | DDL＋日本語 `COMMENT`。`gen_sales_data.py --recreate` が内部で実行する |
| `002_roles_superuser.sql` | **superuser（postgres）** | 参照専用ロール `demo_readonly` の作成（1回だけ） |
| `003_grants.sql` | `ppln_dev_user` | `demo_readonly` への `USAGE`/`SELECT` 付与（何度でも再実行可） |
| `gen_sales_data.py` | `ppln_dev_user` | データ生成＋CSV出力＋`COPY`投入＋整合性検証 |
| `question_candidates.md` | — | 自然に出そうな質問文の候補（正解SQLは無し） |
| `evaluation_questions.json` | — | 評価データ（質問＋正解SQL）。下記「評価データ」参照 |
| `few_shot_examples.json` | — | few-shot 用の例示（質問＋SQL）。下記「few-shot 例示データ」参照 |
| `*.csv` | — | 生成物。`.gitignore` 済み（コミットしない） |

## 前提

- Python 仮想環境 `.venv`（`psycopg2` 同梱）。依存は標準ライブラリ＋`psycopg2` のみ。
- DB 接続情報は環境変数 `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD`
  （`.env` の値。実行ユーザーは `ppln_dev_user`）または引数で上書き。

> **注意**: 2026-08-15 に `demo_sales` の所有権を `admin` から `ppln_dev_user` へ移した。
> `admin` はこのスキーマへの権限を持たないため、**`admin` で実行すると
> `permission denied for schema demo_sales` になる。**

```bash
source .venv/bin/activate
```

## 作り直し手順（この順に実行）

### 1. データ構築（スキーマごと作り直し）

```bash
python sample/sales/gen_sales_data.py --recreate --seed 20260814
```

- `--recreate` … `DROP SCHEMA demo_sales CASCADE` → `001_schema.sql` 実行 → データ投入。
- `--recreate` なし … 既存6テーブルを `TRUNCATE RESTART IDENTITY` して再投入（スキーマは残す）。
- 実行末尾に件数と整合性の検証結果を表示する（すべて「不一致 0」なら成功）。

### 2. 参照専用ロールの作成（superuser で一度だけ）

アプリの実行ユーザー（`ppln_dev_user`）は CREATEROLE を持たないため、ロール作成は superuser で行う。
**superuser で実行するのはこの `CREATE ROLE` 1文だけ。** パスワードはファイルに書かず、
`-v demo_pw=...` で外から渡す（`002_roles_superuser.sql` はコミット対象のため）。

```bash
demo_pw="$(openssl rand -base64 24)"
sudo -u postgres psql -d llm_pipe_lab -v demo_pw="$demo_pw" \
     -f sample/sales/002_roles_superuser.sql
echo "$demo_pw"   # ← この値を後で .env の DEMO_DB_PASSWORD に設定する
```

- 冪等: ロールが既に存在すれば作成せず、パスワードも変更しない。
- `sudo -u postgres psql` はこの環境ではパスワードが必要（対話実行）。

### 3. 権限付与（`ppln_dev_user` で実行）

```bash
PGPASSWORD="$DB_PASSWORD" psql -h localhost -U ppln_dev_user -d llm_pipe_lab \
     -f sample/sales/003_grants.sql
```

- `demo_sales` の `USAGE` と全テーブル `SELECT`、および将来テーブル用の
  `ALTER DEFAULT PRIVILEGES` を付与。`public` スキーマのアプリ用テーブルには一切付与しない。
- `ALTER DEFAULT PRIVILEGES` は**テーブルを作るロール自身**が実行しないと効かない。
  現在の所有者は `ppln_dev_user` なので、この手順も `ppln_dev_user` で行う。

### 4. `.env` に接続情報を設定

`.env.example` に追加済みのキーを、実値で `.env` に設定する（`.env` は追跡対象外）。

```
DEMO_DB_USER=demo_readonly
DEMO_DB_PASSWORD=（手順2で生成した値）
```

## 完了条件の確認

### 件数（`--seed 20260814` 時の実測）

| テーブル | 仕様 | 実測 |
|---|---|---|
| stores | 40 | 40 |
| products | 500 | 500 |
| members | 20,000 | 20,000 |
| sales | 80,000 | 80,000 |
| sale_items | 約200,000 | 198,491 |
| daily_store_summary | 約29,000 | **25,624**（下記参照） |

```sql
SELECT 'stores' t, COUNT(*) FROM demo_sales.stores
UNION ALL SELECT 'products', COUNT(*) FROM demo_sales.products
UNION ALL SELECT 'members', COUNT(*) FROM demo_sales.members
UNION ALL SELECT 'sales', COUNT(*) FROM demo_sales.sales
UNION ALL SELECT 'sale_items', COUNT(*) FROM demo_sales.sale_items
UNION ALL SELECT 'daily_store_summary', COUNT(*) FROM demo_sales.daily_store_summary;
```

### 日本語コメント

```bash
psql -h localhost -U ppln_dev_user -d llm_pipe_lab -c "\d+ demo_sales.sales"
```

全テーブル・全カラムに `COMMENT` が付いている（`Description` 列に表示）。

### 金額の完全一致（いずれも 0 件なら整合）

```sql
-- (a) line_amount = quantity*unit_price - discount_amount
SELECT COUNT(*) FROM demo_sales.sale_items
WHERE line_amount <> quantity*unit_price - discount_amount;

-- (b) sales.total_amount = 明細合計
SELECT COUNT(*) FROM (
  SELECT s.sale_id FROM demo_sales.sales s
  JOIN demo_sales.sale_items i ON i.sale_id = s.sale_id
  GROUP BY s.sale_id, s.total_amount
  HAVING s.total_amount <> SUM(i.line_amount)) x;

-- (c) daily_store_summary = 明細集計
WITH agg AS (
  SELECT s.store_id, s.sold_at::date d,
         SUM(i.line_amount) amt,
         COUNT(DISTINCT s.sale_id) tc,
         COUNT(DISTINCT s.sale_id) FILTER (WHERE s.member_id IS NOT NULL) mtc
  FROM demo_sales.sales s JOIN demo_sales.sale_items i ON i.sale_id=s.sale_id
  GROUP BY s.store_id, s.sold_at::date)
SELECT COUNT(*) FROM demo_sales.daily_store_summary d
FULL JOIN agg a ON a.store_id=d.store_id AND a.d=d.summary_date
WHERE d.sales_amount IS DISTINCT FROM a.amt
   OR d.transaction_count IS DISTINCT FROM a.tc
   OR d.member_transaction_count IS DISTINCT FROM a.mtc;
```

## daily_store_summary の意図的な欠落（重要）

**取引が1件も無かった「店舗×日」は、サマリに行を作っていない。**
サマリだけで日次平均を出すと、取引ゼロの日が母数から落ちて過大になる。これは検証したい
挙動なので、この欠落は埋めていない。

- 対象期間の稼働店舗日（開店〜閉店の範囲内の日数の総和）＝ **27,541**。これがサマリ行数の上限。
- 実際のサマリ行数 = **25,624**（稼働店舗日の約93%。残り約7%が「取引ゼロで行が無い日」）。
- 仕様の「約29,000」は 40店×約730日 からの概算値。実際は 80,000取引 ÷ 稼働店舗日（約27,500）で
  1店1日あたり約2.9件となり、取引ゼロ日を除いた結果このカバレッジになる。**取引総数（80,000）は
  仕様どおり増やしていない**（再生成の速さ優先）。

「取引ゼロの日にサマリ行が無い」ことを利用した検証例:

```sql
-- サマリだけで出した店舗別の日次平均（ゼロ日が母数から抜けるため過大になりうる）
SELECT store_id, ROUND(AVG(sales_amount)) FROM demo_sales.daily_store_summary
GROUP BY store_id ORDER BY store_id;
```

## 仕込んだ「現実のノイズ」と確認方法

このDBには、現実のデータが持つ落とし穴を **7種類、意図して仕込んである。**
きれいなデータで測っても意味が無いから、ではない。**この7種が、このプロジェクトが
「集計ツールではない」ことの根拠**だから（`../../docs/Concept_and_Overview.md` 2節）。

| # | 落とし穴 | 知らないと何が起きるか | 詳細 |
|---|---|---|---|
| 1 | 表記ゆれ（関西/近畿、EC/オンライン） | 関西の売上を出したつもりで、半分の店舗が抜ける | 下記「表記ゆれ対応表」 |
| 2 | 欠損（`birth_date`/`gender`/`floor_area`） | 平均や比率の分母を間違える | 下記「その他のノイズ」 |
| 3 | 非会員取引（`member_id` NULL 40%） | 「会員1人あたり」に非会員が混ざる | 同上 |
| 4 | 論理削除・状態フラグ（`close_date`/`is_discontinued`） | 閉店した店・廃番商品を現役として数える | 同上 |
| 5 | 単価の二重持ち（定価 vs 実売） | 値引きを無視した売上を報告する | 同上 |
| 6 | 粒度違い（`daily_store_summary` に取引ゼロ日の行が無い） | 平均日商が過大に出る | 前節「日次サマリの欠落」 |
| 7 | 日付境界（`sold_at` は時刻あり） | 「12/31 の売上」が23時間ぶんになる | 下記「その他のノイズ」 |

**7種とも、SQL が書けるかどうかとは無関係。** SQL が書ける人でも、このDBの癖を
知らなければ全部間違える。しかも**間違えてもエラーは出ない。それらしい数字が返る。**
評価データ（後述）は、この7種をどれも取りこぼさないよう各2問以上に割り当ててある
（下記「落とし穴の網羅」）。

### 仕込みではない既知の不整合（2026-08-18 発見）

上の7種は**意図して仕込んだもの**。これとは別に、**生成スクリプトの偶発的な不備**による
不整合が2件ある。盲検プロファイリング（[付録05](../../docs/appendix/05_blind_profiling.md)）で見つかった。

| 所見 | 件数 | 母数に対する割合 |
|---|---|---|
| 発売前販売（`sales.sold_at` < `products.launch_date`） | **4,494件** | 明細198,491件の 2.3% |
| 入会前購入（`sales.sold_at` < `members.join_date`） | **4,124件** | 会員取引47,966件の 8.6% |

**原因**: `gen_sales_data.py` が商品・会員を売上日と無関係にランダムへ割り当てているため。
**業務シナリオとして設計したものではない。**

**修正しない。** データを作り直すと実験の再現条件が変わるため、記録に留める。

#### 扱いのルール

- **この2件を「仕込んだ落とし穴」として語らない。** 由来が違う。
  盲検プロファイリングで偶然見つかった**生成スクリプトの不備**である
- **時間軸（`launch_date` / `join_date`）を使う集計・診断パターンを作るときは、
  この2件を除外するかどうかを都度明記する。** 明記が無いと母数が揺れる

### 表記ゆれ対応表

| 列 | 同義の表記 | 混在比率（実測） |
|---|---|---|
| `stores.region` | **「関西」＝「近畿」** | 各4店（半々） |
| `sales.channel` | **「EC」＝「オンライン」**（どちらもネット通販） | 店頭 63,919 (79.9%) / EC 8,096 (10.1%) / オンライン 7,985 (10.0%) |

`channel` は「店頭」「EC」「オンライン」の**3値のみ**（第4の値は作っていない）。
「EC」と「オンライン」を合算して初めて「ネット通販」の正しい母数になる。

```sql
SELECT region, COUNT(*) FROM demo_sales.stores
WHERE region IN ('関西','近畿') GROUP BY region;          -- 関西/近畿 の混在

SELECT channel, COUNT(*) FROM demo_sales.sales GROUP BY channel;  -- EC/オンライン の混在
```

### その他のノイズ（すべて SQL で確認できる）

| # | ノイズ | 確認SQL（要点） | 実測 |
|---|---|---|---|
| 2 | 欠損 | `members` の `birth_date`/`gender` NULL率、`stores.floor_area` NULL率 | 約30% / 約30% / 20% |
| 3 | 非会員取引 | `sales.member_id IS NULL` の割合 | 40.0% |
| 4 | 論理削除・状態 | `products.is_discontinued=true` / `stores.close_date IS NOT NULL` | 約9.6% / 3店 |
| 5 | 単価の二重持ち | `sale_items.unit_price <> products.unit_price` の明細割合 | 29.4% |
| 6 | 日付境界 | `sold_at` は時刻あり（`sold_at::date` と `sold_at` の差） | timestamp 型 |

```sql
-- 例: 「現在営業中の店舗」を正しく絞る（close_date が NULL）
SELECT COUNT(*) FROM demo_sales.stores WHERE close_date IS NULL;

-- 例: 定価と実売がずれている明細
SELECT ROUND(100.0*COUNT(*) FILTER (WHERE i.unit_price<>p.unit_price)/COUNT(*),1)
FROM demo_sales.sale_items i JOIN demo_sales.products p ON p.product_id=i.product_id;
```

## 参照専用ロール demo_readonly の拒否確認

`demo_readonly` で接続し、SELECT は可・書き込み系は拒否されることを確認する。
（`<pw>` は手順2で生成したパスワード）

```bash
# SELECT は可
PGPASSWORD='<pw>' psql -h localhost -U demo_readonly -d llm_pipe_lab \
  -c "SELECT COUNT(*) FROM demo_sales.sales;"

# 以下はすべて権限エラーで拒否されること
PGPASSWORD='<pw>' psql -h localhost -U demo_readonly -d llm_pipe_lab \
  -c "INSERT INTO demo_sales.sales(store_id,sold_at,channel,payment_method,total_amount)
      VALUES (1, now(), '店頭', '現金', 0);"
PGPASSWORD='<pw>' psql -h localhost -U demo_readonly -d llm_pipe_lab \
  -c "UPDATE demo_sales.sales SET total_amount=0 WHERE sale_id=1;"
PGPASSWORD='<pw>' psql -h localhost -U demo_readonly -d llm_pipe_lab \
  -c "DELETE FROM demo_sales.sales WHERE sale_id=1;"
PGPASSWORD='<pw>' psql -h localhost -U demo_readonly -d llm_pipe_lab \
  -c "CREATE TABLE demo_sales.t(x int);"

# アプリ用テーブル（public の nl2sql_experiment）が読めないことも確認
PGPASSWORD='<pw>' psql -h localhost -U demo_readonly -d llm_pipe_lab \
  -c "SELECT COUNT(*) FROM public.nl2sql_experiment;"
```

### 拒否確認の結果（2026-08-14 実測）

`demo_readonly` で接続して確認。SELECT のみ可、書き込み系はすべて権限エラーで拒否された。

| 操作 | 期待 | 実測 |
|---|---|---|
| `SELECT COUNT(*) FROM demo_sales.sales` | 可 | **80000**（成功） |
| `INSERT INTO demo_sales.sales ...` | 拒否 | `ERROR: permission denied for table sales` |
| `UPDATE demo_sales.sales ...` | 拒否 | `ERROR: permission denied for table sales` |
| `DELETE FROM demo_sales.sales ...` | 拒否 | `ERROR: permission denied for table sales` |
| `CREATE TABLE demo_sales.t(...)` | 拒否 | `ERROR: permission denied for schema demo_sales` |
| `SELECT ...` （`public` のアプリ用テーブル） | 拒否 | `ERROR: permission denied for table ...` |

拒否確認の前後で `demo_sales.sales` の件数は 80000 で不変（書き込みが通っていないことを確認）。

**最終行の実測について。** この確認を行った 2026-08-14 時点の `public` 側は、移植元の
RAG 実験ツールのテーブルだった。現在それらは含まれておらず、`public` にあるのは
`nl2sql_experiment` 等である。**確かめた事実は「`demo_readonly` に `public` の権限を
与えていないので拒否される」であり、そこは変わっていない。**
テーブル名を現在のものに書き換えると、実施していない測定を実施したことにするため、
名前は伏せて記録のまま残す。同じ確認は上の手順（`public.nl2sql_experiment` を引く）で再現できる。

## 評価データ（evaluation_questions.json）

NL→SQL の精度を **実行結果一致率** で測るための正解データ（質問＋正解SQL）。
生成SQLの実行結果と、ここに収めた `gold_sql` の実行結果を突き合わせて採点する。


- 形式: JSON 配列。各要素は `id` / `question` / `gold_sql` / `tags` / `note`
  （＋ `ordered`（下記、`true` の問題のみ）／補助的に `difficulty`）。
- **基準日は 2026-08-14 に固定。** 「先月」「直近3か月」等の相対表現は `gold_sql` 内で具体的な
  日付リテラルに展開してある。`CURRENT_DATE` / `now()` / `CURRENT_TIMESTAMP` は不使用。
- 全 `gold_sql` は **`demo_readonly` で実行してエラーが出ないことを確認済み**
  （全テーブルを `demo_sales.` でスキーマ修飾。`demo_readonly` の search_path に `demo_sales` は含まれないため）。

### 用語の定義

質問の答えを一意に定めるための、このデータセットでの語の解釈
。

| 語 | 解釈 |
|---|---|
| 売上 | 税抜・円。`sales.total_amount` の合計 ＝ `sale_items.line_amount` の合計（設計上一致・確認済み、値は同一）。ヘッダ粒度の集計は `total_amount`、商品分類など明細粒度が要る集計は `line_amount` を用いるが結果値は変わらない。 |
| 割合・％ | 「件数ベース」か「売上（金額）ベース」かを**質問文で明記**する。小数は質問文が指定する桁数（例:「小数第1位まで」）で四捨五入し、`gold_sql` の丸めと一致させる。 |
| 客単価 | 売上合計 ÷ 取引件数（取引＝`sales` 1行）。円未満は四捨五入。 |
| 平均日商 | 1日あたり売上。**分母（日数）は質問文で指定**する。「日次店舗サマリを使って」＝売上があった日のみの平均（サマリに取引ゼロ日の行が無い）。q028 ＝取引ゼロの日も暦日として分母に含める。 |
| 上位・トップN | 質問文が示す指標の降順で上位N件。並び順自体は答えに含めない（`ordered` 参照）。 |
| 識別列 | 「〜別／〜ごと」で**店舗・商品・会員**を指す場合、既定は **店舗＝店舗名（`store_name`）／商品＝商品名（`product_name`）／会員＝会員コード（`member_code`）** で識別する。質問文にその語（「店舗名ごと」等）を入れ、正解SQLもその列を出力する。`payment_method`・`member_rank`・`category_l`・`store_type`・`pref`・`gender` などの区分値は、それ自体が識別列で曖昧さは無い。 |
| 出力する列 | 質問文で出す列が数えられるようにする（「商品名と販売数量を」など）。識別列＋集計値の形が基本。 |
| 年代 | `'20代'` のようなテキストで表す。年齢は基準日 2026-08-14 で算出。生年月日未登録・非会員は対象外。 |
| 月別の期間表記 | `'YYYY-MM'` 形式（例 `2026-07`）。質問文にも `（YYYY-MM）` と明記する。 |
| 相対期間 | 「先月」「直近3か月」等は**質問文で境界を決める**（絶対日付か明示範囲）。`note` だけに書かない（プロンプトに渡らないため）。基準日は 2026-08-14。 |
| 表記ゆれ | エリアの「関西」＝「近畿」、チャネルの「ネット通販」＝「EC」＋「オンライン」。 |
| 表示用ラベル | 原則として `gold_sql` で表示用の文字列を作らず生の列値を出す。区分自体が答えでラベルが要る場合（会員／非会員、店頭／ネット通販）は**質問文にラベルを明記**した上で出力する。 |

### 件数と難易度

全 **35問**（易 10 / 中 15 / 難 10）。

### 結果比較の順序判定（`ordered`）

評価側で「生成SQLの結果が正解と一致するか」を判定する際の**行の並び順の扱い**を、
評価データ側で明示する。**既定は「順序を見ない」**（`ordered` キー無し＝`false`）。
**質問文が並び順を要求している問題にだけ `"ordered": true`** を付ける
（判断基準は質問文であって、正解SQLの `ORDER BY` の有無ではない）。

`ordered: true` は次の **6問**（いずれも質問文に「多い順／高い順」と明記）:

| id | 質問（要約） | 理由 |
|---|---|---|
| q003 | 会員数を会員ランク別に**多い順**で | 並び順を明示要求 |
| q011 | 先月の店舗別売上を**多い順**に | 並び順を明示要求 |
| q015 | 商品大分類ごとの売上金額を**多い順**に | 並び順を明示要求 |
| q021 | 関西エリアの店舗別売上を**多い順**に | 並び順を明示要求 |
| q027 | 店舗ごとの平均日商を**高い順**に | 並び順を明示要求 |
| q032 | 店舗形態ごとの面積効率を**高い順**に | 並び順を明示要求 |

**「上位N件（トップ5／トップ10）」は `ordered` を付けない**方針とした（q016・q022・q031）。
「どのN件か」が答えであり、その並び順は問うていないと解釈する（各問の `note` にも記載）。
「月別推移」（q017）も特定の並び順を明示要求していないため `false`。

### `並べ替え` タグの方針

**推奨案を採用**: `並べ替え` タグを「**質問が並び順を要求している**」の意味に統一し、
`ordered: true` の問題にだけ付ける（両者を一致させる）。従来は「正解SQLが `ORDER BY` を
使っている」という意味で付いていたため、見やすさ目的の `ORDER BY` が付く問題
（q004・q006・q010・q016・q017・q022・q031）から `並べ替え` を外し、
タグが無かった q032 に付与した。結果、`並べ替え` タグ＝`ordered: true` の 6問で一致する。

### タグ分布

| タグ | 件数 | | タグ | 件数 |
|---|--:|---|---|--:|
| 集計 | 32 | | 比率 | 9 |
| グループ化 | 22 | | 並べ替え | 6 |
| 結合 | 16 | | 表記ゆれ | 4 |
| 期間_絶対 | 5 | | 欠損 | 4 |
| 上位N | 3 | | 非会員 | 4 |
| 期間_相対 | 2 | | フラグ | 4 |
| 前年比 | 2 | | 単価の取り違え | 2 |
| | | | サマリの欠落 | 2 |

### 落とし穴の網羅（各2問以上）

前述の「仕込んだ現実のノイズ」を、**どれも取りこぼさないよう各2問以上に割り当てて**ある。
1問しか無いと、その1問が偶然当たった／外れただけで落とし穴の攻略可否を誤読する。

| 落とし穴 | 割当問 |
|---|---|
| `region` の「関西／近畿」表記ゆれ | q013, q021 |
| `channel` の「EC／オンライン」表記ゆれ | q012, q018 |
| `birth_date`/`gender` の欠損 | q009, q023, q030, q032 |
| 非会員（`member_id` NULL） | q014, q024, q030, q034 |
| 定価と実売単価の二重持ち | q020, q029 |
| `daily_store_summary` の取引ゼロ日欠落 | q027, q028 |
| 閉店店舗・廃番商品のフラグ | q002, q008, q025, q035 |
| 日付境界（`sold_at` は時刻あり） | q005, q017, q026, q031, q033 |

日付境界は独立したタグを持たないが、`期間_絶対` の問題がこれに当たる。
`gold_sql` はいずれも `sold_at >= '開始' AND sold_at < '翌日/翌月'` の半開区間で書いてあり、
`sold_at::date` や `BETWEEN` で書くと当日の23時台が落ちる。

### 全件の実行確認

```bash
# .env の DEMO_DB_PASSWORD を使う。全 gold_sql を demo_readonly で流してエラーが無いことを確認。
python - <<'PY'
import json
Q=json.load(open("sample/sales/evaluation_questions.json"))
open("/tmp/all_gold.sql","w").write(
  "\\set ON_ERROR_STOP on\n" +
  "".join(f"{q['gold_sql'].rstrip().rstrip(';')};\n" for q in Q))
PY
PGPASSWORD='<demo_readonlyのパスワード>' psql -h localhost -U demo_readonly -d llm_pipe_lab \
  -f /tmp/all_gold.sql >/dev/null && echo OK
```

## few-shot 例示データ（few_shot_examples.json）

`few_shot` パラメータ（プロンプトに例示を何件載せるか）で使う「質問＋SQL」の例示。
`services.load_few_shot()` が読み込む。キーは `question` と `sql` の2つのみ。
**先頭から順に使われる**（`few_shot: 3` なら先頭3件）ので、効かせたい型を前に置いている。

- 件数は **6件**。前半3件が「必須の型」1〜3、後半3件が型4〜6に対応（下表）。
- 全 `sql` は `demo_readonly` で実行してエラーが出ず、**0行にならない**ことを確認済み。
- 質問文は評価データと同じ品質基準（「用語の定義」の5観点：識別列・丸め桁数・集計定義・出力列・絶対日付）で書いている。

| # | 型 | 例示の題材（評価データとは別問題） |
|---|---|---|
| 1 | 副問い合わせで中間値を作ってから使う | 取引ごとの合計数量を副問い合わせで作り、その平均を取る |
| 2 | 結合前に粒度を揃える（二重計上を避ける） | 店舗粒度に集計してから結合し、店舗形態別に平均する |
| 3 | 日次サマリを使う（stores 結合で店舗名） | `daily_store_summary` から店舗名ごとの営業日数 |
| 4 | 絶対日付の半開区間 | 2025年第4四半期（`>= '2025-10-01' AND < '2026-01-01'`） |
| 5 | 比率（分母と桁数を明示） | クレジット払いの件数割合、件数ベース・小数第1位 |
| 6 | 条件付き集計（`FILTER`） | 店舗形態別の取引件数と、そのうちクレジット払い件数 |

### なぜ評価データを流用しないのか

**測る対象に答えを教えることになり、スコアが意味を失うため。** 例示は評価データ35問と
**同じ問題を含めない**（型は同じでも、対象テーブル・列・条件を変える）。
`services.load_few_shot()` は評価データからの流用ができない作りで、例示ファイルが無ければ
黙って空で進まず落ちる（＝測定漏れを防ぐ）。評価データ各問との重複確認は
作成時の実施結果に記録している。

---

## 段階2の判定検証用の質問セット（`rule_validation_questions*.json`）

段階2（曖昧さの判定）を盲検で測るための質問セット。**`id` / `question` / `unique` /
`types` / `basis` / `group_by` / `tables` の7キー。**

`unique` は「その質問文だけで答えが一意に決まるか」、`types` は決まらない場合の型
（1 結合欠落／2 フラグ／3 同義値／4 包括値・NULL）。**型の定義と既定は
`DIAGNOSIS.md` を参照。**

### `_2.json` と `_4.json` の型2ラベルは、旧定義で付いている

**この2ファイルの型2は、確定した定義では当てにならない。**

| ファイル | 作成回 | 型2の読み |
|---|---|---|
| `rule_validation_questions.json`（14件） | 1本目 | `types` キーを持たない旧スキーマ |
| **`rule_validation_questions_2.json`（15件）** | 2本目 | **旧定義（顔ぶれ）** |
| `rule_validation_questions_3.json`（22件） | 3本目 | 開示 |
| **`rule_validation_questions_4.json`（22件）** | 4本目 | **旧定義（顔ぶれ）** |
| `rule_validation_questions_5.json`（22件） | 5本目 | 開示 |
| `rule_validation_questions_6.json`（22件） | 6本目 | 開示 |
| `population_labels.json`（59件） | 別途 | 開示 |

**確定した定義**（`DIAGNOSIS.md`「`types` が指すもの」）はこうである。

> `types` が指すのは「既定を当てて開示すべきか」の一点のみ。
> 出力の顔ぶれが変わるかどうかは型1の内部的な根拠であって、判定基準そのものではない。

**旧定義（顔ぶれ）で読むと、区分値でグループ化した質問の型2が落ちる。**
「店舗形態別の売上合計」で閉店店舗を含めるかは、**行の顔ぶれ（3行）は変えないが値は変える。**
確定した定義では `[2]` だが、旧定義では `[]` になる。

**当て直しは行わない**（中止した）。この2セットは既に読んでおり、
盲検の検証データとしては使い切っている。**当て直しても戻る価値が無い。**

**`_2.json` と `_4.json` を採点に使うときは、型2の数字を「規則の誤り」と読まないこと。**
型1・型3・型4 は定義の影響を受けない。
