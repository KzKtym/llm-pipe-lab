# demo_sales データプロファイリング（盲検）

- 日付: 2026-08-18
- 対象DB/スキーマ: `llm_pipe_lab` / `demo_sales`
- 接続: `demo_readonly`（read-only）経由のDB接続のみ
- クエリ本体: [`profiling.sql`](./profiling.sql)（そのまま `demo_readonly` で再実行可能）

このドキュメントは2段構成。**フェーズ1（盲検プロファイリング）を確定させてから、フェーズ2
（スキーマコメントとの突き合わせ）を追記した。** フェーズ2で得た知識でフェーズ1の記録は
書き換えていない。

> ## 【最優先】食い違い（コメント vs 実データ）: **0件**
> フェーズ2で全テーブル・全列コメントを実データと突き合わせた結果、**コメントの記述が
> 実データと矛盾する項目は無かった**。コメント中の定量的主張（3割NULL、15%退会、1割廃番、
> 2割未計測、閉店3件、明細1〜6、値引大半0、region/channel概ね半々、ヘッダ=明細厳密一致、
> サマリ集計一致）はすべて実数で確認できた。詳細は末尾フェーズ2。

---

## 参照した情報源（盲検の証跡）

フェーズ1・フェーズ2を通じて、参照したのは **DB接続だけ**。既存リポジトリファイル
（`.md` / `.sql` / `.py` / `.json` / `.csv`）は一切開いていない。

| 種別 | 具体的に使ったもの |
|---|---|
| 接続先 | `psql -h localhost -p 5432 -U demo_readonly -d llm_pipe_lab`（読み取り専用） |
| 接続情報の取得 | `.env`（`DEMO_DB_USER` / `DEMO_DB_PASSWORD` のみ。作業指示は依頼書 [付録05](../../docs/appendix/05_blind_profiling.md)） |
| システムビュー | `information_schema.tables`, `information_schema.columns` |
| システムカタログ（構造） | `pg_constraint` + `pg_get_constraintdef()`（PK/FK/UNIQUE の把握。※コメントではない） |
| テーブル本体 | `demo_sales` の全6テーブル（SELECT のみ） |
| コメント（フェーズ2でのみ） | `obj_description()`, `col_description()`（`pg_class` / `pg_attribute` 経由） |

- フェーズ1では **テーブル・列コメントを一切参照していない**（`pg_description` /
  `obj_description` / `col_description` / `\d+` の Description 列は未使用）。
- `demo_sales` への変更は行っていない（DDL/DML なし。全クエリ SELECT）。
- 上表に **DB接続以外（既存リポジトリファイル）は並んでいない**。盲検は保たれている。

---

## 対象スキーマの構造（information_schema / pg_constraint）

6テーブル。行数は次のとおり。

| テーブル | 行数 | 主キー | 一意制約 | 外部キー |
|---|---:|---|---|---|
| `stores` | 40 | `store_id` | `store_code` | — |
| `members` | 20,000 | `member_id` | `member_code` | — |
| `products` | 500 | `product_id` | `product_code` | — |
| `sales` | 80,000 | `sale_id` | — | `store_id→stores`, `member_id→members` |
| `sale_items` | 198,491 | `sale_item_id` | — | `sale_id→sales`, `product_id→products` |
| `daily_store_summary` | 25,624 | (`summary_date`,`store_id`) 複合 | — | `store_id→stores` |

- `sales` → `sale_items` が親子（ヘッダ/明細）。`daily_store_summary` は集計済みテーブル。
- 制約は `information_schema.table_constraints` では **0行**に見えた（`demo_readonly` の
  権限で絞られるため）。構造は `pg_constraint` カタログで把握した。

---

# フェーズ1：盲検プロファイリング（6観点・すべて実数）

## 観点1: 欠損（NULL 件数と率）

全44列を機械的に走査。NULL が出た列だけ抜粋（残りはすべて 0 件）。

| テーブル | 列 | NULL件数 | 母数 | NULL率 |
|---|---|---:|---:|---:|
| `stores` | `close_date` | 37 | 40 | **92.50%** |
| `stores` | `floor_area` | 8 | 40 | **20.00%** |
| `members` | `gender` | 6,008 | 20,000 | **30.04%** |
| `members` | `birth_date` | 5,980 | 20,000 | **29.90%** |
| `sales` | `member_id` | 32,034 | 80,000 | **40.04%** |

- 上記5列以外の **39列は NULL 0件**（NOT NULL 制約と整合）。
- `stores.close_date` の 92.5% NULL は「まだ閉店していない店舗」を表す想定と読める（37/40 が営業中）。
- `sales.member_id` の 40% NULL は「非会員取引」を表す想定と読める（FK は NULL 許容）。
- `members.gender` / `birth_date` の約30% NULL は会員属性の任意入力を示唆。

## 観点2: 一意性（COUNT(*) vs COUNT(DISTINCT)）

主キー以外で **完全一意**だった列：

| テーブル | 列 | 非NULL件数 | distinct | 判定 |
|---|---|---:|---:|---|
| `stores` | `store_code` | 40 | 40 | 一意（UNIQUE制約あり） |
| `stores` | `store_name` | 40 | 40 | 一意（制約なし・実データ上一意） |
| `stores` | `open_date` | 40 | 40 | 一意（制約なし・偶然か設計か不明） |
| `members` | `member_code` | 20,000 | 20,000 | 一意（UNIQUE制約あり） |
| `products` | `product_code` | 500 | 500 | 一意（UNIQUE制約あり） |

**重複のある注目列**：

| テーブル | 列 | 非NULL件数 | distinct | 備考 |
|---|---|---:|---:|---|
| `products` | `product_name` | 500 | 494 | **6件が重複**（`product_code` は一意だが名称は重複） |
| `sales` | `member_id` | 47,966 | 18,207 | 会員は延べ47,966回・実人数18,207人（1人が複数回購入） |
| `sales` | `store_id` | 80,000 | **38** | 全40店中 **38店**しか売上に出現しない |
| `daily_store_summary` | `store_id` | 25,624 | **38** | 同上（集計にも38店のみ） |
| `daily_store_summary` | `summary_date` | 25,624 | 730 | 730日分 |

重複した `product_name`（各2件）：
`ペット用品 スタンダード75` / `家具 お徳用84` / `文具 コンパクト78` /
`書籍 プレミアム82` / `菓子 お徳用95` / `菓子 プレミアム31`。

## 観点3: 値域

### 3-a. 区分値（全値と件数）

**`stores.region`（8種）** — ※`近畿` と `関西` が併存（観点4・突き合わせで詳述）:

| region | 件数 |
|---|---:|
| 関東 | 12 |
| 中部 | 6 |
| 九州 | 5 |
| 近畿 | 4 |
| 関西 | 4 |
| 中国 | 3 |
| 北海道 | 3 |
| 東北 | 3 |

**`stores.store_type`（3種）**: 路面 22 / SC内 9 / 駅ナカ 9
**`stores.pref`（16種）**: 奈良県5, 千葉県4, 東京都4, 鹿児島県4, 北海道3, 岐阜県3, 愛知県3, 兵庫県2, 埼玉県2, 宮城県2, 広島県2, 神奈川県2, 大阪府1, 岡山県1, 福岡県1, 福島県1

**`members.gender`（3種＋NULL）**: 女性 6,814 / 男性 6,641 / **NULL 6,008** / **その他 537**
**`members.member_rank`（3種）**: ブロンズ 11,052 / シルバー 6,309 / ゴールド 2,639
**`members.is_active`（bool）**: true 17,076 / false 2,924
**`members.pref`（20種）**: 東京都3,285 …（`その他` 1,048 を含む。全20値は `profiling.sql` 3-a 出力参照）

**`products.category_l`（6種）**: 趣味・文具100, 食品100, 住居・インテリア75, 家電75, 日用品75, 衣料75
**`products.category_m`（20種）**: すべて 25件ずつ（均等）
**`products.is_discontinued`（bool）**: false 452 / **true 48**

**`sales.channel`（3種）**: 店頭 63,919 / EC 8,096 / オンライン 7,985 （※`EC` と `オンライン` の関係は観点4で詳述）
**`sales.payment_method`（4種）**: 現金 28,049 / クレジット 27,850 / 電子マネー 16,169 / QR 7,932
**`sale_items.quantity`（5種）**: 1→108,884 / 2→49,747 / 3→23,717 / 4→10,202 / 5→5,941

### 3-b. 数値列の min/max/avg（非正値の件数付き）

| 列 | min | max | avg | ≤0(または<0)件数 |
|---|---:|---:|---:|---:|
| `stores.floor_area` | 50 | 560 | 269.69 | 0 |
| `products.unit_price` | 120 | 148,500 | 21,337.06 | 0 |
| `sale_items.unit_price` | 80 | **155,900** | 20,616.00 | 0 |
| `sale_items.discount_amount` | 0 | 139,430 | 676.89 | 0（負値なし） |
| `sale_items.line_amount` | 70 | 742,500 | 35,733.74 | 0 |
| `sales.total_amount` | 80 | 1,454,710 | 88,660.33 | 0 |
| `daily_store_summary.sales_amount` | 120 | 2,457,870 | 276,804.03 | 0 |
| `daily_store_summary.transaction_count` | 1 | 14 | 3.12 | 0 |
| `daily_store_summary.member_transaction_count` | 0 | 10 | 1.87 | 0（負値なし） |

- **`sale_items.unit_price` の min(80)/max(155,900) が、商品マスタ `products.unit_price` の
  min(120)/max(148,500) の範囲を外れる。** 明細の単価がマスタと乖離している（観点4で件数化）。
- 金額・数量に負値やゼロ以下は無し。

### 3-c. 日付/時刻列の min/max（粒度）

| 列 | min | max |
|---|---|---|
| `stores.open_date` | 2008-06-25 | 2023-12-05 |
| `stores.close_date` | 2023-02-06 | 2026-01-27 |
| `members.birth_date` | 1945-01-01 | 2007-12-29 |
| `members.join_date` | 2015-01-01 | 2026-08-14 |
| `products.launch_date` | 2010-01-03 | 2025-12-26 |
| `sales.sold_at` | 2024-08-15 10:04:34 | 2026-08-14 20:57:43 |
| `daily_store_summary.summary_date` | 2024-08-15 | 2026-08-14 |

- `sales.sold_at` は **timestamp（時刻あり）**。他の日付列は date（時刻なし）。
- 取引期間はちょうど2年（2024-08-15 〜 2026-08-14）。

### 3-d. region ラベルの重複（同一 pref が複数 region に跨る）

`stores` の region→pref マッピング：

| region | 所属 pref |
|---|---|
| 近畿 | 大阪府, 奈良県 |
| 関西 | 兵庫県, **奈良県** |

**`奈良県` が `近畿` と `関西` の両方に出現する。** 同一都道府県が2つの地域ラベルに割り当てられている
（`近畿`/`関西` は同一地方を指す表記ゆれと読める）。

### 3-e. members.pref のうち stores.pref に無い値

`その他` / `京都府` / `熊本県` / `静岡県` の4値は `members` にあるが `stores` には無い。
`members.pref` には包括値 **`その他`（1,048件）** が存在する（`stores.pref` には `その他` は無い）。

## 観点4: 列間の整合

### 4-a. FK 参照欠け（孤児行）

| 参照 | 孤児件数 |
|---|---:|
| `sales.store_id → stores` | 0 |
| `sales.member_id → members`（非NULLのみ） | 0 |
| `sale_items.sale_id → sales` | 0 |
| `sale_items.product_id → products` | 0 |
| `daily_store_summary.store_id → stores` | 0 |

**参照整合はすべて成立**（孤児行ゼロ）。

### 4-b〜4-e. 合計・算術の一致

| チェック | 母数 | 一致 | 不一致 |
|---|---:|---:|---:|
| 4-b `sales` に `sale_items` が無い | — | — | **0**（全 sales に明細あり） |
| 4-c `sales.total_amount = SUM(sale_items.line_amount)` | 80,000 | **80,000** | 0 |
| 4-d `line_amount = quantity*unit_price - discount_amount` | 198,491 | **198,491** | 0 |
| 4-e `sale_items.unit_price = products.unit_price`（マスタ） | 198,491 | 140,164 | **58,327** |

- ヘッダ合計と明細合計、明細内の算術は **完全一致**（金額系は内部整合が取れている）。
- **`sale_items.unit_price` はマスタ価格と 58,327件（29.4%）で不一致。**
  内訳: マスタより高い 8,067件 / マスタより低い **50,260件**（大半が安い＝値引き・過去価格を示唆）。

### 4-f. 廃番商品の販売

`is_discontinued = true` の商品を含む明細が **19,028件** 存在する（廃番でも過去の販売実績は残る）。

### 4-g. 時間的整合（発売前・入会前・開店前・閉店後の取引）

| チェック | 件数 |
|---|---:|
| 商品の `launch_date` より前に売れた明細 | **4,494** |
| 会員の `join_date` より前の購入 | **4,124** |
| 店舗の `open_date` より前の取引 | 0 |
| 店舗の `close_date` より後の取引 | 0 |

- **発売前販売 4,494件・入会前購入 4,124件** という時間的にありえない行が存在する。
- 店舗の開店前/閉店後の取引は無し（店舗ライフサイクルは整合）。

### 4-h. store 網羅（売上のない店舗）

売上に出現しない2店舗：

| store_id | code | 店名 | open_date | close_date |
|---:|---|---|---|---|
| 28 | S028 | 広島本通店 | 2021-11-21 | 2024-04-15 |
| 34 | S034 | 函館店 | 2013-03-04 | 2023-02-06 |

いずれも **取引期間開始（2024-08-15）より前に閉店済み**。40店中この2店が売上に出ないため、
`sales` / `daily_store_summary` の店舗は38店になる（観点2の distinct=38 の内訳）。

## 観点5: 時系列

- **取引期間**: `sales.sold_at` = 2024-08-15 10:04:34 〜 2026-08-14 20:57:43（ちょうど2年）。
- **粒度**: `sales.sold_at` は時刻を含む（timestamp）。`daily_store_summary.summary_date` は
  日付のみ（date）。
- **欠けている日**:
  - `sales`: distinct 売上日数 **730** = 暦日数 **730**（**欠損日なし**）。
  - `daily_store_summary`: distinct 日数 **730** = 暦日数 **730**（**欠損日なし**）。

## 観点6: 粒度（集計済みテーブルの再現）

`sales` を store×日で再集計し、`daily_store_summary` と全項目突合：

| 指標 | 結果 |
|---|---|
| `dss` 行数 | 25,624 |
| 再集計行数 | 25,624 |
| 再集計にあり dss に無い | 0 |
| dss にあり 再集計に無い | 0 |
| `sales_amount` 一致 / 不一致 | **25,624 / 0** |
| `transaction_count` 一致 / 不一致 | **25,624 / 0** |
| `member_transaction_count` 一致 / 不一致 | **25,624 / 0** |

- **`daily_store_summary` は明細から完全に再現できる**（3指標すべて全行一致）。
  - `member_transaction_count` = その店・その日の `member_id IS NOT NULL` の取引数と定義が一致。
- **グリッド密度**: 38店 × 730日 = 27,740 の理論値に対し実行数は **25,624**（差 2,116）。
  `daily_store_summary` は「売上ゼロの店舗×日」を行として持たない **疎（sparse）テーブル**。
  差の主因は、期間途中の 2026-01-27 に閉店した store_id=38（上大岡店）を含む、
  取引の無い店舗×日が行として存在しないこと。

## フェーズ1の要点（実数まとめ）

- 欠損: `stores.close_date` 92.5%、`sales.member_id` 40.0%、`members.gender` 30.0%、`members.birth_date` 29.9%。
- 一意性: `product_name` に6件重複。売上に出る店舗は40中**38**。
- 値域: `region` に `近畿`/`関西` 併存（奈良県が両属）。`members` に包括値 `その他`（gender 537 / pref 1,048）。`sale_items.unit_price` がマスタ範囲外。
- 整合: FK孤児ゼロ・ヘッダ/明細/算術は完全一致。一方 **明細単価 vs マスタ単価 29.4%不一致**、**発売前販売4,494**、**入会前購入4,124**、**廃番品販売19,028**。
- 時系列: 2年・欠損日なし。`sold_at` のみ時刻粒度。
- 粒度: `daily_store_summary` は明細から全行再現可（疎テーブル、2,116 店日が非在）。

<!-- フェーズ1ここまで確定。以下フェーズ2はコメント解禁後に追記。 -->

---

# フェーズ2：スキーマコメントとの突き合わせ

フェーズ1確定後に `obj_description()` / `col_description()` でテーブル・列コメントを取得し
（DB接続の一部。既存ファイルは開いていない）、フェーズ1の各所見を3区分した。

## 食い違い（最優先）: 0件

**コメントと実データが矛盾する項目は無い。** コメントに含まれる定量的主張は、フェーズ1／
フェーズ2の実測とすべて一致した。

| コメントの主張 | 実測値 | 判定 |
|---|---|---|
| `members`: birth_date/gender 約3割 NULL | 29.90% / 30.04% | 一致 |
| `members.is_active`: 約15% が false | 14.62%（2,924/20,000） | 一致 |
| `products.is_discontinued`: 約1割 true | 9.6%（48/500） | 一致 |
| `stores.floor_area`: 約2割 NULL | 20.00%（8/40） | 一致 |
| `stores`: 閉店済み3件 | 3件（close_date 非NULL） | 一致 |
| `sale_items`: 1取引あたり1〜6明細 | min 1 / max 6 / avg 2.48 | 一致 |
| `sale_items.discount_amount`: 大半は0 | 84.99%（168,705/198,491）が0 | 一致 |
| `sales.channel`: EC と オンライン 概ね半々 | 8,096 / 7,985 | 一致 |
| `stores.region`: 関西 と 近畿 概ね半々 | 4 / 4 | 一致 |
| `sales.total_amount` = 明細 line_amount 合計に厳密一致 | 80,000/80,000 一致 | 一致 |
| `daily_store_summary`: 明細集計に厳密一致・取引ゼロ店日は行なし | 全行一致・疎グリッド | 一致 |

## 3区分（フェーズ1所見ごと）

### データから見えた（フェーズ1の手順だけで検出）

- 欠損率5列すべて（close_date 92.5% / member_id 40.0% / gender 30.0% / birth_date 29.9% / floor_area 20.0%）。
- `product_name` 6件重複、`product_code` は一意。
- 売上に出る店舗は40中**38**、欠けている2店（store_id 28・34）とその閉店日。
- **`region` の `近畿`/`関西` ラベル併存**（奈良県が両属）— 表記ゆれの存在自体はデータで検出。
- 包括値 `その他`（members.gender 537 / members.pref 1,048）。
- `sale_items.unit_price` がマスタ範囲外、マスタ価格と **29.4%不一致**（安い側50,260件）。
- FK孤児ゼロ、ヘッダ=明細、明細内算術は全一致。
- 廃番品の販売 19,028件。**発売前販売 4,494件・入会前購入 4,124件**。
- 取引期間2年・欠損日なし・`sold_at` のみ時刻粒度。
- `daily_store_summary` は明細から全行再現可、かつ疎グリッド（2,116店日が非在）。

### コメントにしか無い（データを測っても意味は分からない）

- **`unit_price` の意味**: `products.unit_price`＝**定価**、`sale_items.unit_price`＝**実売単価**。
  フェーズ1は29.4%の乖離を測れたが、「定価と実売の差」という**意味**はコメントで初めて分かる。
- **`channel` の EC≡オンライン が同義の表記ゆれ**という断定。データ上は別文字列の2値（奈良県のような
  共有キーが無い）ため、**同義であること自体はデータから証明できず**、コメントで確定する。
- **NULL の意味付け**: `sales.member_id` NULL＝非会員/ゲスト、`gender`/`birth_date` NULL＝未登録、
  `floor_area` NULL＝未計測、`close_date` NULL＝営業中。NULLの存在はデータで見えるが、含意はコメント側。
- `is_active`＝退会・休眠、`is_discontinued`＝取り扱い終了 といった区分値の業務的意味。
- `daily_store_summary` の疎グリッドが**「意図的な欠落」**であること（疎であること自体は測れる）。
- `member_transaction_count` の定義（再現一致は取れたが、定義文はコメント）。

### 食い違い

- **該当なし（0件）。** 上表のとおり。

## フェーズ1では見つからず、コメントには書かれていた項目

下記は、フェーズ1の6観点の走査では表に出さなかったが、コメントに記載があった項目。
（意味の説明そのものは前節「コメントにしか無い」に含めた。ここでは**測れば出たはずだが
フェーズ1で測っていなかった定量項目**と、**注意喚起の記述**を挙げる。）

1. **1取引あたりの明細数レンジ（1〜6）** … フェーズ1は `quantity`(1〜5) は測ったが、
   *sale_idあたりの明細本数* を測っていなかった。フェーズ2で実測 → min1/max6/avg2.48 で一致。
2. **`discount_amount` が0である割合（大半は0）** … フェーズ1は min/max/avg のみ。
   ゼロ割合は未測。フェーズ2で実測 → 84.99%。
3. **`sold_at` の日単位集計での日付境界の注意** … データ事実ではなく運用上の注意喚起。
   フェーズ1の観点には現れない（`sold_at` が時刻粒度である事実自体はフェーズ1で検出済み）。

上記以外に「コメントにあってフェーズ1の測定で全く痕跡が無かった」項目は無い
（表記ゆれ・NULL・乖離・疎グリッド等の**現象はすべてフェーズ1で痕跡を捉えており**、
コメントが足したのは主に**意味付けと同義判定**）。

## フェーズ2で追加参照した情報源

- `obj_description('demo_sales.<table>'::regclass)` 相当（`pg_class` 経由）… テーブルコメント
- `col_description('demo_sales.<table>'::regclass, ordinal)` 相当（`pg_attribute` 経由）… 列コメント
- 上記はいずれもDB接続の一部。**フェーズ2でも既存リポジトリファイルは開いていない。**
