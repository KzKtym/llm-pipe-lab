-- ============================================================================
-- 001_schema.sql
--   demo_sales スキーマ: NL→SQL 検証用デモ売上データベースの DDL + 日本語コメント
--   実行ユーザー: admin（スキーマ・全テーブルの所有者になる）
--   このファイルはロールを一切作成しない。参照専用ロールは 002/003 を参照。
--   COMMENT はそのまま LLM への入力になるため、値の意味・単位を明記している。
--   金額はすべて税抜・円（整数）。
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS demo_sales;
COMMENT ON SCHEMA demo_sales IS 'NL→SQL 検証用のデモ売上データ（架空の小売チェーン）。アプリ用テーブル（public の nl2sql_experiment 等）とは分離する。';

SET search_path TO demo_sales;

-- ----------------------------------------------------------------------------
-- 1. stores（店舗マスタ）40件
-- ----------------------------------------------------------------------------
CREATE TABLE stores (
    store_id    serial PRIMARY KEY,
    store_code  text    NOT NULL UNIQUE,
    store_name  text    NOT NULL,
    region      text    NOT NULL,
    pref        text    NOT NULL,
    store_type  text    NOT NULL,
    open_date   date    NOT NULL,
    close_date  date,
    floor_area  numeric
);
COMMENT ON TABLE  stores            IS '店舗マスタ。全40店舗。閉店済みの店舗（close_date あり）を3件含む。';
COMMENT ON COLUMN stores.store_id   IS '店舗ID（主キー、連番）';
COMMENT ON COLUMN stores.store_code IS '店舗コード。''S001'' 形式の一意な文字列';
COMMENT ON COLUMN stores.store_name IS '店舗名。「〇〇店」形式';
COMMENT ON COLUMN stores.region     IS '広域エリア区分（関東・中部・関西/近畿・中国・九州・東北・北海道 等）。「関西」と「近畿」は同義の表記ゆれが混在する（概ね半々）';
COMMENT ON COLUMN stores.pref       IS '所在地の都道府県名（例: ''東京都''・''大阪府''）';
COMMENT ON COLUMN stores.store_type IS '店舗形態。''路面'' / ''SC内''（ショッピングセンター内）/ ''駅ナカ'' のいずれか';
COMMENT ON COLUMN stores.open_date  IS '開店日';
COMMENT ON COLUMN stores.close_date IS '閉店日。営業中の店舗は NULL。NULL でなければ閉店済み';
COMMENT ON COLUMN stores.floor_area IS '売場面積（平方メートル）。約2割が NULL（未計測）';

-- ----------------------------------------------------------------------------
-- 2. products（商品マスタ）500件
-- ----------------------------------------------------------------------------
CREATE TABLE products (
    product_id      serial PRIMARY KEY,
    product_code    text    NOT NULL UNIQUE,
    product_name    text    NOT NULL,
    category_l      text    NOT NULL,
    category_m      text    NOT NULL,
    unit_price      numeric NOT NULL,
    launch_date     date    NOT NULL,
    is_discontinued boolean NOT NULL
);
COMMENT ON TABLE  products                 IS '商品マスタ。全500件。unit_price は定価であり、実売価格ではない点に注意（実売は sale_items.unit_price）';
COMMENT ON COLUMN products.product_id      IS '商品ID（主キー、連番）';
COMMENT ON COLUMN products.product_code    IS '商品コード。''P00001'' 形式の一意な文字列';
COMMENT ON COLUMN products.product_name    IS '商品名';
COMMENT ON COLUMN products.category_l      IS '商品大分類（6種）。食品 / 日用品 / 衣料 / 家電 / 住居・インテリア / 趣味・文具';
COMMENT ON COLUMN products.category_m      IS '商品中分類（20種）。category_l を細分化した分類';
COMMENT ON COLUMN products.unit_price      IS '定価（税抜、円）。実際の販売単価は sale_items.unit_price を参照（一致しない場合がある）';
COMMENT ON COLUMN products.launch_date     IS '発売日';
COMMENT ON COLUMN products.is_discontinued IS '廃番フラグ。true=廃番（取り扱い終了）。全体の約1割が true';

-- ----------------------------------------------------------------------------
-- 3. members（会員マスタ）20,000件
-- ----------------------------------------------------------------------------
CREATE TABLE members (
    member_id   serial PRIMARY KEY,
    member_code text    NOT NULL UNIQUE,
    birth_date  date,
    gender      text,
    pref        text    NOT NULL,
    join_date   date    NOT NULL,
    member_rank text    NOT NULL,
    is_active   boolean NOT NULL
);
COMMENT ON TABLE  members             IS '会員マスタ。全20,000件。birth_date と gender はそれぞれ約3割が NULL（未登録）';
COMMENT ON COLUMN members.member_id   IS '会員ID（主キー、連番）';
COMMENT ON COLUMN members.member_code IS '会員コード。''M000001'' 形式の一意な文字列';
COMMENT ON COLUMN members.birth_date  IS '生年月日。約3割が NULL（未登録）。年齢分析の母数に注意';
COMMENT ON COLUMN members.gender      IS '性別。''男性'' / ''女性'' / ''その他'' のいずれか。約3割が NULL（未登録）';
COMMENT ON COLUMN members.pref        IS '会員の居住都道府県名';
COMMENT ON COLUMN members.join_date   IS '入会日';
COMMENT ON COLUMN members.member_rank IS '会員ランク。''ブロンズ'' / ''シルバー'' / ''ゴールド'' のいずれか';
COMMENT ON COLUMN members.is_active   IS '有効会員フラグ。true=有効。約15%が false（退会・休眠）';

-- ----------------------------------------------------------------------------
-- 4. sales（取引ヘッダ）80,000件
-- ----------------------------------------------------------------------------
CREATE TABLE sales (
    sale_id        serial PRIMARY KEY,
    store_id       integer   NOT NULL REFERENCES stores(store_id),
    member_id      integer   REFERENCES members(member_id),
    sold_at        timestamp NOT NULL,
    channel        text      NOT NULL,
    payment_method text      NOT NULL,
    total_amount   numeric   NOT NULL
);
COMMENT ON TABLE  sales                IS '取引ヘッダ。全80,000件。直近2年分に分布。total_amount は明細(sale_items)の line_amount 合計と厳密に一致する';
COMMENT ON COLUMN sales.sale_id        IS '取引ID（主キー、連番）';
COMMENT ON COLUMN sales.store_id       IS '取引を行った店舗ID（stores.store_id への外部キー）';
COMMENT ON COLUMN sales.member_id      IS '会員ID（members.member_id への外部キー）。約4割が NULL＝非会員（ゲスト）取引。会員分析の母数に注意';
COMMENT ON COLUMN sales.sold_at        IS '取引日時（日付＋時刻）。時刻を含むため、日単位集計では日付境界の扱いに注意';
COMMENT ON COLUMN sales.channel        IS '販売チャネル。''店頭'' / ''EC'' / ''オンライン''。「EC」と「オンライン」は同義の表記ゆれ（どちらもネット通販、概ね半々）';
COMMENT ON COLUMN sales.payment_method IS '支払方法。''現金'' / ''クレジット'' / ''電子マネー'' / ''QR'' のいずれか';
COMMENT ON COLUMN sales.total_amount   IS '取引金額の合計（税抜、円）。当取引の sale_items.line_amount の合計に一致';

-- ----------------------------------------------------------------------------
-- 5. sale_items（取引明細）約200,000件
-- ----------------------------------------------------------------------------
CREATE TABLE sale_items (
    sale_item_id    serial PRIMARY KEY,
    sale_id         integer NOT NULL REFERENCES sales(sale_id),
    product_id      integer NOT NULL REFERENCES products(product_id),
    quantity        integer NOT NULL,
    unit_price      numeric NOT NULL,
    discount_amount numeric NOT NULL,
    line_amount     numeric NOT NULL
);
COMMENT ON TABLE  sale_items                 IS '取引明細。約200,000件。1取引あたり1〜6明細。unit_price は実売単価で、products.unit_price（定価）と一致しない場合がある';
COMMENT ON COLUMN sale_items.sale_item_id    IS '明細ID（主キー、連番）';
COMMENT ON COLUMN sale_items.sale_id         IS '取引ID（sales.sale_id への外部キー）';
COMMENT ON COLUMN sale_items.product_id      IS '商品ID（products.product_id への外部キー）';
COMMENT ON COLUMN sale_items.quantity        IS '購入数量（1〜5）';
COMMENT ON COLUMN sale_items.unit_price      IS '実売単価（税抜、円）。値引前の1個あたり販売価格。定価 products.unit_price と一致しないことがある';
COMMENT ON COLUMN sale_items.discount_amount IS '当明細の値引額（税抜、円）。大半は0。明細単位（数量込み）の値引額';
COMMENT ON COLUMN sale_items.line_amount     IS '明細金額（数量×実売単価−値引額、税抜、円）。quantity×unit_price−discount_amount に一致';

-- ----------------------------------------------------------------------------
-- 6. daily_store_summary（日次店舗サマリ）約29,000件
-- ----------------------------------------------------------------------------
CREATE TABLE daily_store_summary (
    summary_date             date    NOT NULL,
    store_id                 integer NOT NULL REFERENCES stores(store_id),
    sales_amount             numeric NOT NULL,
    transaction_count        integer NOT NULL,
    member_transaction_count integer NOT NULL,
    PRIMARY KEY (summary_date, store_id)
);
COMMENT ON TABLE  daily_store_summary                          IS '日次×店舗の売上サマリ。取引が1件以上あった (日付, 店舗) の組のみ行が存在する（取引ゼロの店舗日は行が無い＝意図的な欠落）。明細からの集計と厳密に一致する';
COMMENT ON COLUMN daily_store_summary.summary_date             IS '集計対象日（sold_at の日付部分）';
COMMENT ON COLUMN daily_store_summary.store_id                 IS '店舗ID（stores.store_id への外部キー）';
COMMENT ON COLUMN daily_store_summary.sales_amount             IS '当日・当店の売上金額（税抜、円）。当日・当店の sale_items.line_amount 合計に一致';
COMMENT ON COLUMN daily_store_summary.transaction_count        IS '当日・当店の取引件数（sales の件数）';
COMMENT ON COLUMN daily_store_summary.member_transaction_count IS '当日・当店の取引のうち会員取引（member_id が非NULL）の件数';
