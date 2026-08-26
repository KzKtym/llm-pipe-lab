-- ============================================================================
-- profiling.sql  --  demo_sales blind data profiling (Phase 1 + Phase 2)
-- ----------------------------------------------------------------------------
-- 実行: PGPASSWORD="<demo_readonly pw>" \
--         psql -h localhost -p 5432 -U demo_readonly -d llm_pipe_lab \
--         -P pager=off -f sample/sales/profiling.sql
--
-- 前提: demo_readonly は read-only。全テーブルを demo_sales. でスキーマ修飾する
--       (demo_readonly の search_path に demo_sales は含まれない)。
-- 情報源: DB接続のみ (information_schema / テーブル本体 / pg_constraint)。
--         Phase 1 はテーブル・列コメントを参照しない。
--         Phase 2 (末尾) で obj_description / col_description を解禁する。
-- demo_sales を作り直しても同じ手順を再実行できる。副作用のあるDDL/DMLは無し。
-- ============================================================================


-- ============================================================================
-- STRUCTURE 0: テーブル・列・制約の把握 (information_schema / pg_constraint)
-- ============================================================================

-- 0-a. テーブル一覧
SELECT table_name
FROM information_schema.tables
WHERE table_schema='demo_sales'
ORDER BY table_name;

-- 0-b. 列と型
SELECT table_name, ordinal_position AS pos, column_name, data_type,
       numeric_precision AS nprec, numeric_scale AS nscale, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema='demo_sales'
ORDER BY table_name, ordinal_position;

-- 0-c. 制約 (PK / FK / UNIQUE)。※ pg_constraint は構造カタログでありコメントではない。
--      information_schema.table_constraints は権限で 0 行に絞られるため catalog を使う。
SELECT conrelid::regclass AS tbl, conname, contype, pg_get_constraintdef(oid) AS def
FROM pg_constraint
WHERE connamespace='demo_sales'::regnamespace
ORDER BY conrelid::regclass::text, contype;

-- 0-d. 行数
SELECT 'stores' t, count(*) n FROM demo_sales.stores
UNION ALL SELECT 'members',             count(*) FROM demo_sales.members
UNION ALL SELECT 'products',            count(*) FROM demo_sales.products
UNION ALL SELECT 'sales',               count(*) FROM demo_sales.sales
UNION ALL SELECT 'sale_items',          count(*) FROM demo_sales.sale_items
UNION ALL SELECT 'daily_store_summary', count(*) FROM demo_sales.daily_store_summary;


-- ============================================================================
-- 観点1: 欠損 (各テーブル・各列の NULL 件数と率)  -- 全44列を機械的に走査
-- ============================================================================
SELECT tbl, col, nulls, total, round(100.0*nulls/total,2) AS null_pct FROM (
  SELECT 'stores' tbl,'store_id' col, count(*) FILTER (WHERE store_id IS NULL) nulls, count(*) total FROM demo_sales.stores
  UNION ALL SELECT 'stores','store_code', count(*) FILTER (WHERE store_code IS NULL), count(*) FROM demo_sales.stores
  UNION ALL SELECT 'stores','store_name', count(*) FILTER (WHERE store_name IS NULL), count(*) FROM demo_sales.stores
  UNION ALL SELECT 'stores','region', count(*) FILTER (WHERE region IS NULL), count(*) FROM demo_sales.stores
  UNION ALL SELECT 'stores','pref', count(*) FILTER (WHERE pref IS NULL), count(*) FROM demo_sales.stores
  UNION ALL SELECT 'stores','store_type', count(*) FILTER (WHERE store_type IS NULL), count(*) FROM demo_sales.stores
  UNION ALL SELECT 'stores','open_date', count(*) FILTER (WHERE open_date IS NULL), count(*) FROM demo_sales.stores
  UNION ALL SELECT 'stores','close_date', count(*) FILTER (WHERE close_date IS NULL), count(*) FROM demo_sales.stores
  UNION ALL SELECT 'stores','floor_area', count(*) FILTER (WHERE floor_area IS NULL), count(*) FROM demo_sales.stores
  UNION ALL SELECT 'members','member_id', count(*) FILTER (WHERE member_id IS NULL), count(*) FROM demo_sales.members
  UNION ALL SELECT 'members','member_code', count(*) FILTER (WHERE member_code IS NULL), count(*) FROM demo_sales.members
  UNION ALL SELECT 'members','birth_date', count(*) FILTER (WHERE birth_date IS NULL), count(*) FROM demo_sales.members
  UNION ALL SELECT 'members','gender', count(*) FILTER (WHERE gender IS NULL), count(*) FROM demo_sales.members
  UNION ALL SELECT 'members','pref', count(*) FILTER (WHERE pref IS NULL), count(*) FROM demo_sales.members
  UNION ALL SELECT 'members','join_date', count(*) FILTER (WHERE join_date IS NULL), count(*) FROM demo_sales.members
  UNION ALL SELECT 'members','member_rank', count(*) FILTER (WHERE member_rank IS NULL), count(*) FROM demo_sales.members
  UNION ALL SELECT 'members','is_active', count(*) FILTER (WHERE is_active IS NULL), count(*) FROM demo_sales.members
  UNION ALL SELECT 'products','product_id', count(*) FILTER (WHERE product_id IS NULL), count(*) FROM demo_sales.products
  UNION ALL SELECT 'products','product_code', count(*) FILTER (WHERE product_code IS NULL), count(*) FROM demo_sales.products
  UNION ALL SELECT 'products','product_name', count(*) FILTER (WHERE product_name IS NULL), count(*) FROM demo_sales.products
  UNION ALL SELECT 'products','category_l', count(*) FILTER (WHERE category_l IS NULL), count(*) FROM demo_sales.products
  UNION ALL SELECT 'products','category_m', count(*) FILTER (WHERE category_m IS NULL), count(*) FROM demo_sales.products
  UNION ALL SELECT 'products','unit_price', count(*) FILTER (WHERE unit_price IS NULL), count(*) FROM demo_sales.products
  UNION ALL SELECT 'products','launch_date', count(*) FILTER (WHERE launch_date IS NULL), count(*) FROM demo_sales.products
  UNION ALL SELECT 'products','is_discontinued', count(*) FILTER (WHERE is_discontinued IS NULL), count(*) FROM demo_sales.products
  UNION ALL SELECT 'sales','sale_id', count(*) FILTER (WHERE sale_id IS NULL), count(*) FROM demo_sales.sales
  UNION ALL SELECT 'sales','store_id', count(*) FILTER (WHERE store_id IS NULL), count(*) FROM demo_sales.sales
  UNION ALL SELECT 'sales','member_id', count(*) FILTER (WHERE member_id IS NULL), count(*) FROM demo_sales.sales
  UNION ALL SELECT 'sales','sold_at', count(*) FILTER (WHERE sold_at IS NULL), count(*) FROM demo_sales.sales
  UNION ALL SELECT 'sales','channel', count(*) FILTER (WHERE channel IS NULL), count(*) FROM demo_sales.sales
  UNION ALL SELECT 'sales','payment_method', count(*) FILTER (WHERE payment_method IS NULL), count(*) FROM demo_sales.sales
  UNION ALL SELECT 'sales','total_amount', count(*) FILTER (WHERE total_amount IS NULL), count(*) FROM demo_sales.sales
  UNION ALL SELECT 'sale_items','sale_item_id', count(*) FILTER (WHERE sale_item_id IS NULL), count(*) FROM demo_sales.sale_items
  UNION ALL SELECT 'sale_items','sale_id', count(*) FILTER (WHERE sale_id IS NULL), count(*) FROM demo_sales.sale_items
  UNION ALL SELECT 'sale_items','product_id', count(*) FILTER (WHERE product_id IS NULL), count(*) FROM demo_sales.sale_items
  UNION ALL SELECT 'sale_items','quantity', count(*) FILTER (WHERE quantity IS NULL), count(*) FROM demo_sales.sale_items
  UNION ALL SELECT 'sale_items','unit_price', count(*) FILTER (WHERE unit_price IS NULL), count(*) FROM demo_sales.sale_items
  UNION ALL SELECT 'sale_items','discount_amount', count(*) FILTER (WHERE discount_amount IS NULL), count(*) FROM demo_sales.sale_items
  UNION ALL SELECT 'sale_items','line_amount', count(*) FILTER (WHERE line_amount IS NULL), count(*) FROM demo_sales.sale_items
  UNION ALL SELECT 'daily_store_summary','summary_date', count(*) FILTER (WHERE summary_date IS NULL), count(*) FROM demo_sales.daily_store_summary
  UNION ALL SELECT 'daily_store_summary','store_id', count(*) FILTER (WHERE store_id IS NULL), count(*) FROM demo_sales.daily_store_summary
  UNION ALL SELECT 'daily_store_summary','sales_amount', count(*) FILTER (WHERE sales_amount IS NULL), count(*) FROM demo_sales.daily_store_summary
  UNION ALL SELECT 'daily_store_summary','transaction_count', count(*) FILTER (WHERE transaction_count IS NULL), count(*) FROM demo_sales.daily_store_summary
  UNION ALL SELECT 'daily_store_summary','member_transaction_count', count(*) FILTER (WHERE member_transaction_count IS NULL), count(*) FROM demo_sales.daily_store_summary
) x ORDER BY tbl, col;


-- ============================================================================
-- 観点2: 一意性 (COUNT(*) vs COUNT(DISTINCT))  -- PK以外の候補・重複列を検出
-- ============================================================================
SELECT tbl, col, total, non_null, distinct_vals, (non_null=distinct_vals AND non_null>0) AS is_unique FROM (
  SELECT 'stores' tbl,'store_code' col, count(*) total, count(store_code) non_null, count(DISTINCT store_code) distinct_vals FROM demo_sales.stores
  UNION ALL SELECT 'stores','store_name',count(*),count(store_name),count(DISTINCT store_name) FROM demo_sales.stores
  UNION ALL SELECT 'stores','region',count(*),count(region),count(DISTINCT region) FROM demo_sales.stores
  UNION ALL SELECT 'stores','pref',count(*),count(pref),count(DISTINCT pref) FROM demo_sales.stores
  UNION ALL SELECT 'stores','store_type',count(*),count(store_type),count(DISTINCT store_type) FROM demo_sales.stores
  UNION ALL SELECT 'stores','open_date',count(*),count(open_date),count(DISTINCT open_date) FROM demo_sales.stores
  UNION ALL SELECT 'stores','floor_area',count(*),count(floor_area),count(DISTINCT floor_area) FROM demo_sales.stores
  UNION ALL SELECT 'members','member_code',count(*),count(member_code),count(DISTINCT member_code) FROM demo_sales.members
  UNION ALL SELECT 'members','birth_date',count(*),count(birth_date),count(DISTINCT birth_date) FROM demo_sales.members
  UNION ALL SELECT 'members','gender',count(*),count(gender),count(DISTINCT gender) FROM demo_sales.members
  UNION ALL SELECT 'members','pref',count(*),count(pref),count(DISTINCT pref) FROM demo_sales.members
  UNION ALL SELECT 'members','member_rank',count(*),count(member_rank),count(DISTINCT member_rank) FROM demo_sales.members
  UNION ALL SELECT 'products','product_code',count(*),count(product_code),count(DISTINCT product_code) FROM demo_sales.products
  UNION ALL SELECT 'products','product_name',count(*),count(product_name),count(DISTINCT product_name) FROM demo_sales.products
  UNION ALL SELECT 'products','category_l',count(*),count(category_l),count(DISTINCT category_l) FROM demo_sales.products
  UNION ALL SELECT 'products','category_m',count(*),count(category_m),count(DISTINCT category_m) FROM demo_sales.products
  UNION ALL SELECT 'products','unit_price',count(*),count(unit_price),count(DISTINCT unit_price) FROM demo_sales.products
  UNION ALL SELECT 'sales','store_id',count(*),count(store_id),count(DISTINCT store_id) FROM demo_sales.sales
  UNION ALL SELECT 'sales','member_id',count(*),count(member_id),count(DISTINCT member_id) FROM demo_sales.sales
  UNION ALL SELECT 'sales','channel',count(*),count(channel),count(DISTINCT channel) FROM demo_sales.sales
  UNION ALL SELECT 'sales','payment_method',count(*),count(payment_method),count(DISTINCT payment_method) FROM demo_sales.sales
  UNION ALL SELECT 'sale_items','sale_id',count(*),count(sale_id),count(DISTINCT sale_id) FROM demo_sales.sale_items
  UNION ALL SELECT 'sale_items','product_id',count(*),count(product_id),count(DISTINCT product_id) FROM demo_sales.sale_items
  UNION ALL SELECT 'sale_items','quantity',count(*),count(quantity),count(DISTINCT quantity) FROM demo_sales.sale_items
  UNION ALL SELECT 'daily_store_summary','store_id',count(*),count(store_id),count(DISTINCT store_id) FROM demo_sales.daily_store_summary
  UNION ALL SELECT 'daily_store_summary','summary_date',count(*),count(summary_date),count(DISTINCT summary_date) FROM demo_sales.daily_store_summary
) x ORDER BY tbl, col;

-- 2-b. products.product_name の重複 (product_code は UNIQUE だが name は非一意)
SELECT product_name, count(*) FROM demo_sales.products
GROUP BY product_name HAVING count(*)>1 ORDER BY 2 DESC, 1;


-- ============================================================================
-- 観点3: 値域  -- 低カーディナリティ区分値は全値と件数、数値/日付は min/max/分布
-- ============================================================================

-- 3-a. 区分値の全値と件数
SELECT region,         count(*) FROM demo_sales.stores   GROUP BY region         ORDER BY 2 DESC, 1;
SELECT store_type,     count(*) FROM demo_sales.stores   GROUP BY store_type     ORDER BY 2 DESC, 1;
SELECT pref,           count(*) FROM demo_sales.stores   GROUP BY pref           ORDER BY 2 DESC, 1;
SELECT gender,         count(*) FROM demo_sales.members  GROUP BY gender         ORDER BY 2 DESC, 1;
SELECT member_rank,    count(*) FROM demo_sales.members  GROUP BY member_rank    ORDER BY 2 DESC, 1;
SELECT is_active,      count(*) FROM demo_sales.members  GROUP BY is_active      ORDER BY 2 DESC, 1;
SELECT pref,           count(*) FROM demo_sales.members  GROUP BY pref           ORDER BY 2 DESC, 1;
SELECT category_l,     count(*) FROM demo_sales.products GROUP BY category_l     ORDER BY 2 DESC, 1;
SELECT category_m,     count(*) FROM demo_sales.products GROUP BY category_m     ORDER BY 2 DESC, 1;
SELECT is_discontinued,count(*) FROM demo_sales.products GROUP BY is_discontinued ORDER BY 2 DESC, 1;
SELECT channel,        count(*) FROM demo_sales.sales    GROUP BY channel        ORDER BY 2 DESC, 1;
SELECT payment_method, count(*) FROM demo_sales.sales    GROUP BY payment_method ORDER BY 2 DESC, 1;
SELECT quantity,       count(*) FROM demo_sales.sale_items GROUP BY quantity     ORDER BY 1;

-- 3-b. 数値列の min/max/avg と 非正値(<=0)の件数
SELECT 'stores.floor_area' col, min(floor_area)::text mn, max(floor_area)::text mx, round(avg(floor_area),2)::text av, count(*) FILTER (WHERE floor_area<=0) nonpos FROM demo_sales.stores
UNION ALL SELECT 'products.unit_price', min(unit_price)::text,max(unit_price)::text,round(avg(unit_price),2)::text,count(*) FILTER (WHERE unit_price<=0) FROM demo_sales.products
UNION ALL SELECT 'sale_items.unit_price', min(unit_price)::text,max(unit_price)::text,round(avg(unit_price),2)::text,count(*) FILTER (WHERE unit_price<=0) FROM demo_sales.sale_items
UNION ALL SELECT 'sale_items.discount_amount', min(discount_amount)::text,max(discount_amount)::text,round(avg(discount_amount),2)::text,count(*) FILTER (WHERE discount_amount<0) FROM demo_sales.sale_items
UNION ALL SELECT 'sale_items.line_amount', min(line_amount)::text,max(line_amount)::text,round(avg(line_amount),2)::text,count(*) FILTER (WHERE line_amount<=0) FROM demo_sales.sale_items
UNION ALL SELECT 'sales.total_amount', min(total_amount)::text,max(total_amount)::text,round(avg(total_amount),2)::text,count(*) FILTER (WHERE total_amount<=0) FROM demo_sales.sales
UNION ALL SELECT 'dss.sales_amount', min(sales_amount)::text,max(sales_amount)::text,round(avg(sales_amount),2)::text,count(*) FILTER (WHERE sales_amount<=0) FROM demo_sales.daily_store_summary
UNION ALL SELECT 'dss.transaction_count', min(transaction_count)::text,max(transaction_count)::text,round(avg(transaction_count),2)::text,count(*) FILTER (WHERE transaction_count<=0) FROM demo_sales.daily_store_summary
UNION ALL SELECT 'dss.member_transaction_count', min(member_transaction_count)::text,max(member_transaction_count)::text,round(avg(member_transaction_count),2)::text,count(*) FILTER (WHERE member_transaction_count<0) FROM demo_sales.daily_store_summary;

-- 3-c. 日付/時刻列の min/max
SELECT 'stores.open_date' col, min(open_date)::text mn, max(open_date)::text mx FROM demo_sales.stores
UNION ALL SELECT 'stores.close_date', min(close_date)::text, max(close_date)::text FROM demo_sales.stores
UNION ALL SELECT 'members.birth_date', min(birth_date)::text, max(birth_date)::text FROM demo_sales.members
UNION ALL SELECT 'members.join_date', min(join_date)::text, max(join_date)::text FROM demo_sales.members
UNION ALL SELECT 'products.launch_date', min(launch_date)::text, max(launch_date)::text FROM demo_sales.products
UNION ALL SELECT 'sales.sold_at', min(sold_at)::text, max(sold_at)::text FROM demo_sales.sales
UNION ALL SELECT 'dss.summary_date', min(summary_date)::text, max(summary_date)::text FROM demo_sales.daily_store_summary;

-- 3-d. region ラベルの重複 (同一 pref が複数 region に跨るか)
SELECT region, string_agg(DISTINCT pref, ', ' ORDER BY pref) prefs, count(*) stores
FROM demo_sales.stores GROUP BY region ORDER BY region;
SELECT pref, string_agg(DISTINCT region, ', ' ORDER BY region) regions
FROM demo_sales.stores GROUP BY pref HAVING count(DISTINCT region) > 1 ORDER BY pref;

-- 3-e. members.pref のうち stores.pref に無い値 (区分値ドメインの差)
SELECT DISTINCT m.pref FROM demo_sales.members m
WHERE NOT EXISTS (SELECT 1 FROM demo_sales.stores s WHERE s.pref=m.pref) ORDER BY 1;


-- ============================================================================
-- 観点4: 列間の整合  -- FK参照欠け、合計一致、親子件数、マスタとの突合
-- ============================================================================

-- 4-a. FK 孤児行 (参照欠け)
SELECT 'sales.store_id->stores' rel, count(*) orphans FROM demo_sales.sales s LEFT JOIN demo_sales.stores d ON s.store_id=d.store_id WHERE d.store_id IS NULL
UNION ALL SELECT 'sales.member_id->members', count(*) FROM demo_sales.sales s LEFT JOIN demo_sales.members m ON s.member_id=m.member_id WHERE s.member_id IS NOT NULL AND m.member_id IS NULL
UNION ALL SELECT 'sale_items.sale_id->sales', count(*) FROM demo_sales.sale_items si LEFT JOIN demo_sales.sales s ON si.sale_id=s.sale_id WHERE s.sale_id IS NULL
UNION ALL SELECT 'sale_items.product_id->products', count(*) FROM demo_sales.sale_items si LEFT JOIN demo_sales.products p ON si.product_id=p.product_id WHERE p.product_id IS NULL
UNION ALL SELECT 'dss.store_id->stores', count(*) FROM demo_sales.daily_store_summary d LEFT JOIN demo_sales.stores st ON d.store_id=st.store_id WHERE st.store_id IS NULL;

-- 4-b. 親に子が無い行 (sales に sale_items が無い)
SELECT count(*) sales_without_items FROM demo_sales.sales s
WHERE NOT EXISTS (SELECT 1 FROM demo_sales.sale_items si WHERE si.sale_id=s.sale_id);

-- 4-c. ヘッダ合計 = 明細合計 (sales.total_amount vs SUM(sale_items.line_amount))
SELECT count(*) total_sales,
       count(*) FILTER (WHERE s.total_amount = agg.sum_line) match_cnt,
       count(*) FILTER (WHERE s.total_amount <> agg.sum_line) mismatch_cnt
FROM demo_sales.sales s
JOIN (SELECT sale_id, sum(line_amount) sum_line FROM demo_sales.sale_items GROUP BY sale_id) agg ON s.sale_id=agg.sale_id;

-- 4-d. 明細内算術 (line_amount = quantity*unit_price - discount_amount)
SELECT count(*) total,
       count(*) FILTER (WHERE line_amount = quantity*unit_price - discount_amount) match_cnt,
       count(*) FILTER (WHERE line_amount <> quantity*unit_price - discount_amount) mismatch_cnt
FROM demo_sales.sale_items;

-- 4-e. sale_items.unit_price vs products.unit_price (マスタ価格との一致率と方向)
SELECT count(*) total,
       count(*) FILTER (WHERE si.unit_price = p.unit_price) match_cnt,
       count(*) FILTER (WHERE si.unit_price <> p.unit_price) mismatch_cnt,
       count(*) FILTER (WHERE si.unit_price > p.unit_price) higher_than_master,
       count(*) FILTER (WHERE si.unit_price < p.unit_price) lower_than_master
FROM demo_sales.sale_items si JOIN demo_sales.products p ON si.product_id=p.product_id;

-- 4-f. 廃番商品(is_discontinued)の販売明細
SELECT count(*) items_of_discontinued
FROM demo_sales.sale_items si JOIN demo_sales.products p ON si.product_id=p.product_id
WHERE p.is_discontinued;

-- 4-g. 時間的整合: 発売前・入会前・開店前・閉店後の取引
SELECT 'items_before_product_launch' chk, count(*) n
FROM demo_sales.sale_items si JOIN demo_sales.sales s ON si.sale_id=s.sale_id JOIN demo_sales.products p ON si.product_id=p.product_id
WHERE (s.sold_at)::date < p.launch_date
UNION ALL
SELECT 'sales_before_member_join', count(*)
FROM demo_sales.sales s JOIN demo_sales.members m ON s.member_id=m.member_id WHERE (s.sold_at)::date < m.join_date
UNION ALL
SELECT 'sales_before_store_open', count(*)
FROM demo_sales.sales s JOIN demo_sales.stores st ON s.store_id=st.store_id WHERE (s.sold_at)::date < st.open_date
UNION ALL
SELECT 'sales_after_store_close', count(*)
FROM demo_sales.sales s JOIN demo_sales.stores st ON s.store_id=st.store_id WHERE st.close_date IS NOT NULL AND (s.sold_at)::date > st.close_date;

-- 4-h. store 網羅: 売上のない店舗 (stores 40 vs sales 38)
SELECT st.store_id, st.store_code, st.store_name, st.open_date, st.close_date
FROM demo_sales.stores st
WHERE NOT EXISTS (SELECT 1 FROM demo_sales.sales s WHERE s.store_id=st.store_id)
ORDER BY st.store_id;


-- ============================================================================
-- 観点5: 時系列  -- 期間・粒度(時刻の有無)・欠けている日
-- ============================================================================

-- 5-a. 日次網羅: sales の distinct 日数 vs 暦日数 (欠けている日の有無)
SELECT count(DISTINCT (sold_at)::date) AS distinct_sale_days,
       (max((sold_at)::date) - min((sold_at)::date) + 1) AS calendar_days,
       min(sold_at) AS first_ts, max(sold_at) AS last_ts
FROM demo_sales.sales;

-- 5-b. daily_store_summary の日次網羅 (summary_date は date 型 = 時刻粒度なし)
SELECT count(DISTINCT summary_date) AS distinct_days,
       (max(summary_date) - min(summary_date) + 1) AS calendar_days
FROM demo_sales.daily_store_summary;


-- ============================================================================
-- 観点6: 粒度  -- 集計済み daily_store_summary を明細から作り直して突合
-- ============================================================================

-- 6-a. sales から store×日 で再集計し dss と全項目突合
WITH rebuilt AS (
  SELECT store_id, (sold_at)::date AS d,
         sum(total_amount) AS amt,
         count(*) AS txn,
         count(*) FILTER (WHERE member_id IS NOT NULL) AS mtxn
  FROM demo_sales.sales GROUP BY store_id, (sold_at)::date
)
SELECT
  (SELECT count(*) FROM demo_sales.daily_store_summary) AS dss_rows,
  (SELECT count(*) FROM rebuilt) AS rebuilt_rows,
  count(*) FILTER (WHERE d.store_id IS NULL) AS in_rebuilt_not_dss,
  count(*) FILTER (WHERE r.store_id IS NULL) AS in_dss_not_rebuilt,
  count(*) FILTER (WHERE d.sales_amount = r.amt)              AS amt_match,
  count(*) FILTER (WHERE d.sales_amount <> r.amt)             AS amt_mismatch,
  count(*) FILTER (WHERE d.transaction_count = r.txn)         AS txn_match,
  count(*) FILTER (WHERE d.transaction_count <> r.txn)        AS txn_mismatch,
  count(*) FILTER (WHERE d.member_transaction_count = r.mtxn) AS mtxn_match,
  count(*) FILTER (WHERE d.member_transaction_count <> r.mtxn) AS mtxn_mismatch
FROM demo_sales.daily_store_summary d
FULL OUTER JOIN rebuilt r ON d.store_id=r.store_id AND d.summary_date=r.d;

-- 6-b. dss の store×日 グリッド密度 (疎か密か)
SELECT count(DISTINCT store_id) AS stores_in_dss,
       count(DISTINCT summary_date) AS days_in_dss,
       count(*) AS actual_rows,
       count(DISTINCT store_id)*count(DISTINCT summary_date) AS full_grid
FROM demo_sales.daily_store_summary;


-- ============================================================================
-- PHASE 2: スキーマコメントの解禁 (obj_description / col_description)
-- ----------------------------------------------------------------------------
-- ここから下は Phase 1 を確定した後にのみ実行する。DB接続の一部でありファイルは開かない。
-- ============================================================================

-- P2-a. テーブルコメント
SELECT c.relname AS table_name, obj_description(c.oid) AS table_comment
FROM pg_class c
WHERE c.relnamespace='demo_sales'::regnamespace AND c.relkind='r'
ORDER BY c.relname;

-- P2-b. 列コメント (全列)
SELECT c.relname AS table_name, a.attnum AS pos, a.attname AS column_name,
       col_description(c.oid, a.attnum) AS column_comment
FROM pg_class c
JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
WHERE c.relnamespace='demo_sales'::regnamespace AND c.relkind='r'
ORDER BY c.relname, a.attnum;

-- P2-c. コメント主張の実数検証 (フェーズ1で未測定だった項目)
-- 1取引あたりの明細数 (コメント: 1〜6明細)
SELECT min(cnt) mn, max(cnt) mx, round(avg(cnt),2) av
FROM (SELECT sale_id, count(*) cnt FROM demo_sales.sale_items GROUP BY sale_id) t;
SELECT cnt AS items_per_sale, count(*) AS sales
FROM (SELECT sale_id, count(*) cnt FROM demo_sales.sale_items GROUP BY sale_id) t
GROUP BY cnt ORDER BY cnt;
-- discount_amount=0 の割合 (コメント: 大半は0)
SELECT count(*) total, count(*) FILTER (WHERE discount_amount=0) zero_cnt,
       round(100.0*count(*) FILTER (WHERE discount_amount=0)/count(*),2) zero_pct
FROM demo_sales.sale_items;
-- 閉店店舗数 (コメント: 閉店済み3件)
SELECT count(*) FILTER (WHERE close_date IS NOT NULL) closed FROM demo_sales.stores;
