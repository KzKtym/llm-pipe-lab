#!/usr/bin/env python3
"""
demo_sales デモ売上データの生成・投入スクリプト。

依存: 標準ライブラリ + psycopg2 のみ。
特徴:
  - 乱数シード固定（--seed、既定 20260814）。何度実行しても同一データ。
  - 生成 → CSV 出力 → COPY で投入（INSERT ループは使わない）。
  - --recreate でスキーマごと作り直し（001_schema.sql を実行）。
  - 金額整合を厳密化:
      sales.total_amount            = 当取引の sale_items.line_amount 合計
      daily_store_summary.*         = 明細からの集計と一致
      sale_items.line_amount        = quantity*unit_price - discount_amount
  - 「直近2年」の基準日は再現性のため固定（REF_DATE）。

使い方:
  python gen_sales_data.py --recreate           # スキーマ作り直し＋投入
  python gen_sales_data.py                        # 既存スキーマを TRUNCATE ＋再投入
  python gen_sales_data.py --recreate --seed 20260814
接続情報は環境変数 DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD（既定は
localhost:5432 / llm_pipe_lab / admin / admin）、または引数で上書き可。
"""

import argparse
import csv
import os
from datetime import date, datetime, timedelta

import psycopg2

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
SCHEMA = "demo_sales"
DEFAULT_SEED = 20260814

# 「直近2年」の基準日。再現性のため固定（実行日に依存させない）。
REF_DATE = date(2026, 8, 14)
WINDOW_DAYS = 730
WINDOW_END = REF_DATE
WINDOW_START = REF_DATE - timedelta(days=WINDOW_DAYS - 1)  # 2024-08-15

N_STORES = 40
N_PRODUCTS = 500
N_MEMBERS = 20_000
N_SALES = 80_000

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_SQL = os.path.join(HERE, "001_schema.sql")

TABLES_IN_LOAD_ORDER = [
    "stores",
    "products",
    "members",
    "sales",
    "sale_items",
    "daily_store_summary",
]

# ---------------------------------------------------------------------------
# マスタ用の語彙
# ---------------------------------------------------------------------------
# 広域エリア → 都道府県。件数の合計は 40（店舗数）に一致させる。
# 「関西」エリアは表記ゆれとして「関西」「近畿」を概ね半々で割り当てる。
REGION_DEF = [
    # (エリアキー, 表示ラベル or None, [都道府県], 店舗数)
    ("関東", "関東", ["東京都", "神奈川県", "埼玉県", "千葉県"], 12),
    ("関西", None, ["大阪府", "京都府", "兵庫県", "奈良県"], 8),  # ラベルは 関西/近畿
    ("中部", "中部", ["愛知県", "静岡県", "岐阜県"], 6),
    ("九州", "九州", ["福岡県", "熊本県", "鹿児島県"], 5),
    ("中国", "中国", ["広島県", "岡山県"], 3),
    ("東北", "東北", ["宮城県", "福島県"], 3),
    ("北海道", "北海道", ["北海道"], 3),
]

# 店名に使う地区名（40件を重複なく取れるだけ用意）
AREA_NAMES = [
    "梅田", "難波", "天王寺", "京橋", "三宮", "四条河原町", "奈良", "堺",
    "新宿", "渋谷", "池袋", "銀座", "横浜", "川崎", "大宮", "船橋", "柏", "町田",
    "名古屋栄", "金山", "浜松", "静岡", "岐阜",
    "博多", "天神", "熊本", "鹿児島中央",
    "広島本通", "岡山",
    "仙台", "郡山",
    "札幌", "旭川", "函館",
    "吉祥寺", "自由が丘", "北千住", "上大岡", "藤沢", "たまプラーザ",
    "西宮北口", "高槻", "枚方", "姫路",
]

STORE_TYPES = ["路面", "SC内", "駅ナカ"]

# 商品分類: 大分類(6) → 中分類(20)
CATEGORY_TREE = {
    "食品": ["生鮮食品", "加工食品", "菓子", "飲料"],
    "日用品": ["トイレタリー", "洗剤・掃除用品", "ペーパー類"],
    "衣料": ["メンズ衣料", "レディース衣料", "キッズ衣料"],
    "家電": ["生活家電", "AV機器", "情報機器"],
    "住居・インテリア": ["家具", "寝具", "キッチン用品"],
    "趣味・文具": ["文具", "書籍", "スポーツ用品", "ペット用品"],
}
# 大分類ごとの価格帯（税抜・円）
PRICE_BAND = {
    "食品": (100, 2000),
    "日用品": (100, 3000),
    "衣料": (1000, 15000),
    "家電": (3000, 150000),
    "住居・インテリア": (1000, 50000),
    "趣味・文具": (100, 20000),
}

# 会員の都道府県（人口を意識した重み）
MEMBER_PREF = [
    ("東京都", 18), ("神奈川県", 10), ("大阪府", 10), ("愛知県", 8),
    ("埼玉県", 8), ("千葉県", 7), ("兵庫県", 6), ("福岡県", 6),
    ("北海道", 5), ("京都府", 4), ("広島県", 3), ("宮城県", 3),
    ("静岡県", 4), ("岡山県", 2), ("熊本県", 2), ("奈良県", 2),
    ("岐阜県", 2), ("鹿児島県", 2), ("福島県", 2), ("その他", 6),
]

GENDERS = ["男性", "女性", "その他"]
GENDER_WEIGHTS = [48, 48, 4]
RANKS = ["ブロンズ", "シルバー", "ゴールド"]
RANK_WEIGHTS = [55, 32, 13]

PAYMENTS = ["現金", "クレジット", "電子マネー", "QR"]
PAYMENT_WEIGHTS = [35, 35, 20, 10]

# チャネル: 店頭 が大半。EC と オンライン はネット通販の同義表記ゆれ（概ね半々）。
# 生成後の実比率は README に対応表として記載する。

# 曜日係数（月曜が低い / 週末が高い）: index=weekday() 0=月..6=日
WEEKDAY_FACTOR = [0.80, 0.90, 0.95, 0.95, 1.10, 1.30, 1.25]
# 月係数（12月が高い / 年始と真冬がやや低い）: index=month-1
MONTH_FACTOR = [0.85, 0.85, 1.00, 1.00, 1.00, 0.95,
                1.10, 1.10, 1.00, 1.05, 1.10, 1.40]

# 1取引あたりの明細数の分布（平均 ~2.5 → 明細 約200,000件）
ITEMS_COUNT_CHOICES = [1, 2, 3, 4, 5, 6]
ITEMS_COUNT_WEIGHTS = [28, 30, 20, 13, 6, 3]

QTY_CHOICES = [1, 2, 3, 4, 5]
QTY_WEIGHTS = [55, 25, 12, 5, 3]


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------
def rand_date(rng, start: date, end: date) -> date:
    """start〜end（両端含む）の一様な日付。"""
    span = (end - start).days
    return start + timedelta(days=rng.randint(0, span))


def round_to(value: float, unit: int) -> int:
    return int(round(value / unit) * unit)


# ---------------------------------------------------------------------------
# マスタ生成
# ---------------------------------------------------------------------------
def gen_stores(rng):
    """40店舗。3件を閉店済みに。region に 関西/近畿 の表記ゆれ。floor_area 2割 NULL。"""
    # エリア割り当て（合計40）を作る
    area_slots = []
    for key, label, prefs, cnt in REGION_DEF:
        area_slots.extend([(key, label, prefs)] * cnt)
    assert len(area_slots) == N_STORES

    areas = AREA_NAMES[:N_STORES]

    # 関西エリアは 4件「関西」/ 4件「近畿」に割る
    kansai_labels = ["関西", "関西", "関西", "関西", "近畿", "近畿", "近畿", "近畿"]
    rng.shuffle(kansai_labels)
    kansai_iter = iter(kansai_labels)

    # 閉店店舗にする store_id を3件選ぶ
    closed_ids = set(rng.sample(range(1, N_STORES + 1), 3))
    # floor_area を NULL にする店舗をちょうど2割（8件）選ぶ（小さい母数のため件数固定）
    null_area_ids = set(rng.sample(range(1, N_STORES + 1), round(N_STORES * 0.20)))

    rows = []
    for i in range(N_STORES):
        sid = i + 1
        key, label, prefs = area_slots[i]
        region = label if label is not None else next(kansai_iter)
        pref = rng.choice(prefs)
        stype = rng.choices(STORE_TYPES, weights=[45, 40, 15])[0]
        open_date = rand_date(rng, date(2008, 1, 1), date(2023, 12, 31))
        close_date = None
        if sid in closed_ids:
            earliest = open_date + timedelta(days=365 * 2)
            if earliest < WINDOW_END:
                close_date = rand_date(rng, earliest, WINDOW_END)
            else:
                close_date = rand_date(rng, open_date + timedelta(days=200), WINDOW_END)
        # 売場面積: 2割 NULL（件数固定）、店舗形態で規模を変える
        if sid in null_area_ids:
            floor_area = None
        else:
            base = {"路面": (120, 600), "SC内": (150, 450), "駅ナカ": (40, 160)}[stype]
            floor_area = round_to(rng.uniform(*base), 10)
        rows.append({
            "store_id": sid,
            "store_code": f"S{sid:03d}",
            "store_name": f"{areas[i]}店",
            "region": region,
            "pref": pref,
            "store_type": stype,
            "open_date": open_date,
            "close_date": close_date,
            "floor_area": floor_area,
        })
    return rows


def gen_products(rng):
    """500商品。中分類20種にほぼ均等配分。unit_price は定価。1割 廃番。"""
    mids = []  # (category_l, category_m)
    for cl, cms in CATEGORY_TREE.items():
        for cm in cms:
            mids.append((cl, cm))
    assert len(mids) == 20

    rows = []
    for i in range(N_PRODUCTS):
        pid = i + 1
        cl, cm = mids[i % len(mids)]
        lo, hi = PRICE_BAND[cl]
        # 価格は対数っぽく散らし、10円丸め（家電など高額は100円丸め）
        raw = rng.uniform(lo, hi)
        unit = 100 if hi >= 30000 else 10
        unit_price = max(unit, round_to(raw, unit))
        rows.append({
            "product_id": pid,
            "product_code": f"P{pid:05d}",
            "product_name": f"{cm} {rng.choice(['スタンダード','プレミアム','お徳用','限定','定番','コンパクト'])}{rng.randint(1,99):02d}",
            "category_l": cl,
            "category_m": cm,
            "unit_price": unit_price,
            "launch_date": rand_date(rng, date(2010, 1, 1), date(2025, 12, 31)),
            "is_discontinued": rng.random() < 0.10,
        })
    return rows


def gen_members(rng):
    """20,000会員。birth_date/gender 3割 NULL。is_active 15% false。"""
    prefs = [p for p, _ in MEMBER_PREF]
    pref_w = [w for _, w in MEMBER_PREF]
    rows = []
    for i in range(N_MEMBERS):
        mid = i + 1
        birth_date = None
        if rng.random() >= 0.30:
            birth_date = rand_date(rng, date(1945, 1, 1), date(2007, 12, 31))
        gender = None
        if rng.random() >= 0.30:
            gender = rng.choices(GENDERS, weights=GENDER_WEIGHTS)[0]
        rows.append({
            "member_id": mid,
            "member_code": f"M{mid:06d}",
            "birth_date": birth_date,
            "gender": gender,
            "pref": rng.choices(prefs, weights=pref_w)[0],
            "join_date": rand_date(rng, date(2015, 1, 1), WINDOW_END),
            "member_rank": rng.choices(RANKS, weights=RANK_WEIGHTS)[0],
            "is_active": rng.random() >= 0.15,
        })
    return rows


# ---------------------------------------------------------------------------
# トランザクション生成
# ---------------------------------------------------------------------------
def build_store_day_candidates(rng, stores):
    """(store_id, date) の候補と重みを作る。営業中の日のみ。"""
    # 店舗ごとの人気度（0.85〜1.25）。偏りを抑え、店舗日カバレッジを確保する
    store_pop = {s["store_id"]: rng.uniform(0.85, 1.25) for s in stores}
    candidates = []  # (store_id, date)
    weights = []
    for s in stores:
        sid = s["store_id"]
        start = max(WINDOW_START, s["open_date"])
        end = WINDOW_END
        if s["close_date"] is not None:
            end = min(end, s["close_date"])
        if start > end:
            continue
        pop = store_pop[sid]
        d = start
        one = timedelta(days=1)
        while d <= end:
            w = pop * WEEKDAY_FACTOR[d.weekday()] * MONTH_FACTOR[d.month - 1]
            candidates.append((sid, d))
            weights.append(w)
            d += one
    return candidates, weights


def gen_sales_and_items(rng, stores, products):
    """sales / sale_items を生成し、daily_store_summary を集計して返す。"""
    candidates, weights = build_store_day_candidates(rng, stores)

    # 80,000取引の (店舗, 日) をまとめて重み付き抽選
    picks = rng.choices(candidates, weights=weights, k=N_SALES)

    prod_price = {p["product_id"]: p["unit_price"] for p in products}
    product_ids = [p["product_id"] for p in products]

    sales = []
    items = []
    # 集計: (store_id, date) -> [sales_amount, txn_count, member_txn_count]
    summary = {}

    sale_item_id = 0
    for idx, (store_id, d) in enumerate(picks):
        sale_id = idx + 1

        # 時刻（営業時間 10:00〜20:59）
        hh = rng.randint(10, 20)
        mm = rng.randint(0, 59)
        ss = rng.randint(0, 59)
        sold_at = datetime(d.year, d.month, d.day, hh, mm, ss)

        # 会員 / 非会員（4割 NULL）
        member_id = None
        if rng.random() >= 0.40:
            member_id = rng.randint(1, N_MEMBERS)

        # チャネル（店頭80% / EC10% / オンライン10%）
        r = rng.random()
        if r < 0.80:
            channel = "店頭"
        elif r < 0.90:
            channel = "EC"
        else:
            channel = "オンライン"

        payment = rng.choices(PAYMENTS, weights=PAYMENT_WEIGHTS)[0]

        # 明細
        n_items = rng.choices(ITEMS_COUNT_CHOICES, weights=ITEMS_COUNT_WEIGHTS)[0]
        total = 0
        for _ in range(n_items):
            sale_item_id += 1
            pid = rng.choice(product_ids)
            qty = rng.choices(QTY_CHOICES, weights=QTY_WEIGHTS)[0]
            list_price = prod_price[pid]

            # 実売単価: 7割は定価どおり、3割はずらす（多くは値下げ、稀に高め）
            if rng.random() < 0.70:
                sell_price = list_price
            else:
                factor = rng.uniform(0.70, 1.05)
                unit = 100 if list_price >= 30000 else 10
                sell_price = max(unit, round_to(list_price * factor, unit))

            # 値引: 85%は0、15%は小額値引（明細単位）
            subtotal = qty * sell_price
            if rng.random() < 0.15:
                rate = rng.uniform(0.05, 0.20)
                discount = round_to(subtotal * rate, 10)
                discount = min(discount, subtotal)  # 明細金額が負にならないよう上限
            else:
                discount = 0

            line_amount = subtotal - discount
            total += line_amount
            items.append({
                "sale_item_id": sale_item_id,
                "sale_id": sale_id,
                "product_id": pid,
                "quantity": qty,
                "unit_price": sell_price,
                "discount_amount": discount,
                "line_amount": line_amount,
            })

        sales.append({
            "sale_id": sale_id,
            "store_id": store_id,
            "member_id": member_id,
            "sold_at": sold_at,
            "channel": channel,
            "payment_method": payment,
            "total_amount": total,
        })

        # 日次サマリ集計
        key = (d, store_id)
        agg = summary.get(key)
        if agg is None:
            agg = [0, 0, 0]
            summary[key] = agg
        agg[0] += total
        agg[1] += 1
        if member_id is not None:
            agg[2] += 1

    summary_rows = []
    for (d, store_id) in sorted(summary.keys()):
        amt, tc, mtc = summary[(d, store_id)]
        summary_rows.append({
            "summary_date": d,
            "store_id": store_id,
            "sales_amount": amt,
            "transaction_count": tc,
            "member_transaction_count": mtc,
        })

    return sales, items, summary_rows


# ---------------------------------------------------------------------------
# CSV 出力
# ---------------------------------------------------------------------------
COLUMNS = {
    "stores": ["store_id", "store_code", "store_name", "region", "pref",
               "store_type", "open_date", "close_date", "floor_area"],
    "products": ["product_id", "product_code", "product_name", "category_l",
                 "category_m", "unit_price", "launch_date", "is_discontinued"],
    "members": ["member_id", "member_code", "birth_date", "gender", "pref",
                "join_date", "member_rank", "is_active"],
    "sales": ["sale_id", "store_id", "member_id", "sold_at", "channel",
              "payment_method", "total_amount"],
    "sale_items": ["sale_item_id", "sale_id", "product_id", "quantity",
                   "unit_price", "discount_amount", "line_amount"],
    "daily_store_summary": ["summary_date", "store_id", "sales_amount",
                            "transaction_count", "member_transaction_count"],
}


def _fmt(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def write_csv(path, table, rows):
    cols = COLUMNS[table]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for r in rows:
            w.writerow([_fmt(r[c]) for c in cols])


# ---------------------------------------------------------------------------
# DB 投入
# ---------------------------------------------------------------------------
def connect(args):
    return psycopg2.connect(
        host=args.host, port=args.port, dbname=args.dbname,
        user=args.user, password=args.password,
    )


def recreate_schema(conn):
    with open(SCHEMA_SQL, encoding="utf-8") as f:
        ddl = f.read()
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;")
        cur.execute(ddl)
    conn.commit()


def truncate_all(conn):
    with conn.cursor() as cur:
        cur.execute(
            f"TRUNCATE {', '.join(f'{SCHEMA}.{t}' for t in TABLES_IN_LOAD_ORDER)} "
            f"RESTART IDENTITY CASCADE;"
        )
    conn.commit()


def copy_table(conn, table, csv_path):
    cols = ", ".join(COLUMNS[table])
    sql = (f"COPY {SCHEMA}.{table} ({cols}) "
           f"FROM STDIN WITH (FORMAT csv, NULL '')")
    with conn.cursor() as cur, open(csv_path, encoding="utf-8") as f:
        cur.copy_expert(sql, f)
    conn.commit()


def reset_sequences(conn):
    seqs = {
        "stores": "store_id", "products": "product_id", "members": "member_id",
        "sales": "sale_id", "sale_items": "sale_item_id",
    }
    with conn.cursor() as cur:
        for table, col in seqs.items():
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{SCHEMA}.{table}', '{col}'), "
                f"COALESCE((SELECT MAX({col}) FROM {SCHEMA}.{table}), 1));"
            )
    conn.commit()


# ---------------------------------------------------------------------------
# 検証
# ---------------------------------------------------------------------------
def verify(conn):
    checks = []
    with conn.cursor() as cur:
        # 件数
        for t in TABLES_IN_LOAD_ORDER:
            cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.{t};")
            checks.append((f"件数 {t}", cur.fetchone()[0]))

        # 整合性1: line_amount = quantity*unit_price - discount_amount
        cur.execute(f"""
            SELECT COUNT(*) FROM {SCHEMA}.sale_items
            WHERE line_amount <> quantity*unit_price - discount_amount;""")
        checks.append(("line_amount 式の不一致（0が正常）", cur.fetchone()[0]))

        # 整合性2: sales.total_amount = 明細合計
        cur.execute(f"""
            SELECT COUNT(*) FROM (
              SELECT s.sale_id
              FROM {SCHEMA}.sales s
              JOIN {SCHEMA}.sale_items i ON i.sale_id = s.sale_id
              GROUP BY s.sale_id, s.total_amount
              HAVING s.total_amount <> SUM(i.line_amount)
            ) x;""")
        checks.append(("total_amount と明細合計の不一致（0が正常）", cur.fetchone()[0]))

        # 整合性3: daily_store_summary = 明細集計
        cur.execute(f"""
            WITH agg AS (
              SELECT s.store_id, s.sold_at::date AS d,
                     SUM(i.line_amount) AS amt,
                     COUNT(DISTINCT s.sale_id) AS tc,
                     COUNT(DISTINCT s.sale_id) FILTER (WHERE s.member_id IS NOT NULL) AS mtc
              FROM {SCHEMA}.sales s
              JOIN {SCHEMA}.sale_items i ON i.sale_id = s.sale_id
              GROUP BY s.store_id, s.sold_at::date
            )
            SELECT COUNT(*) FROM {SCHEMA}.daily_store_summary d
            FULL JOIN agg a ON a.store_id = d.store_id AND a.d = d.summary_date
            WHERE d.sales_amount IS DISTINCT FROM a.amt
               OR d.transaction_count IS DISTINCT FROM a.tc
               OR d.member_transaction_count IS DISTINCT FROM a.mtc;""")
        checks.append(("daily_store_summary と明細集計の不一致（0が正常）", cur.fetchone()[0]))

        # ノイズ確認
        cur.execute(f"SELECT region, COUNT(*) FROM {SCHEMA}.stores "
                    f"WHERE region IN ('関西','近畿') GROUP BY region ORDER BY region;")
        checks.append(("region 関西/近畿 の分布", dict(cur.fetchall())))

        cur.execute(f"SELECT channel, COUNT(*) FROM {SCHEMA}.sales "
                    f"GROUP BY channel ORDER BY channel;")
        checks.append(("channel の分布", dict(cur.fetchall())))

        cur.execute(f"SELECT ROUND(100.0*COUNT(*) FILTER (WHERE member_id IS NULL)/COUNT(*),1) "
                    f"FROM {SCHEMA}.sales;")
        checks.append(("非会員取引の割合(%)", cur.fetchone()[0]))

        cur.execute(f"SELECT ROUND(100.0*COUNT(*) FILTER (WHERE birth_date IS NULL)/COUNT(*),1), "
                    f"ROUND(100.0*COUNT(*) FILTER (WHERE gender IS NULL)/COUNT(*),1) "
                    f"FROM {SCHEMA}.members;")
        row = cur.fetchone()
        checks.append(("会員 birth_date NULL率 / gender NULL率(%)", row))

        cur.execute(f"SELECT ROUND(100.0*COUNT(*) FILTER (WHERE floor_area IS NULL)/COUNT(*),1) "
                    f"FROM {SCHEMA}.stores;")
        checks.append(("店舗 floor_area NULL率(%)", cur.fetchone()[0]))

        cur.execute(f"SELECT ROUND(100.0*COUNT(*) FILTER (WHERE is_discontinued)/COUNT(*),1) "
                    f"FROM {SCHEMA}.products;")
        checks.append(("廃番商品の割合(%)", cur.fetchone()[0]))

        cur.execute(f"SELECT COUNT(*) FROM {SCHEMA}.stores WHERE close_date IS NOT NULL;")
        checks.append(("閉店済み店舗数", cur.fetchone()[0]))

        # 定価と実売がずれている明細の割合
        cur.execute(f"""
            SELECT ROUND(100.0*COUNT(*) FILTER (WHERE i.unit_price <> p.unit_price)/COUNT(*),1)
            FROM {SCHEMA}.sale_items i JOIN {SCHEMA}.products p ON p.product_id=i.product_id;""")
        checks.append(("実売単価≠定価 の明細割合(%)", cur.fetchone()[0]))

    return checks


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="demo_sales データ生成・投入")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--recreate", action="store_true",
                    help="スキーマを DROP して 001_schema.sql で作り直す")
    ap.add_argument("--outdir", default=HERE, help="CSV 出力先（既定はこのファイルと同じ場所）")
    ap.add_argument("--keep-csv", action="store_true", default=True,
                    help="投入後も CSV を残す（既定: 残す。.gitignore 済み）")
    ap.add_argument("--host", default=os.environ.get("DB_HOST", "localhost"))
    ap.add_argument("--port", default=os.environ.get("DB_PORT", "5432"))
    ap.add_argument("--dbname", default=os.environ.get("DB_NAME", "llm_pipe_lab"))
    ap.add_argument("--user", default=os.environ.get("DB_USER", "admin"))
    ap.add_argument("--password", default=os.environ.get("DB_PASSWORD", "admin"))
    args = ap.parse_args()

    import random
    rng = random.Random(args.seed)

    print(f"[gen] seed={args.seed}  期間 {WINDOW_START}〜{WINDOW_END}")

    print("[gen] マスタ生成 ...")
    stores = gen_stores(rng)
    products = gen_products(rng)
    members = gen_members(rng)

    print("[gen] 取引・明細生成 ...")
    sales, items, summary = gen_sales_and_items(rng, stores, products)
    print(f"[gen]   sales={len(sales):,}  sale_items={len(items):,}  "
          f"daily_store_summary={len(summary):,}")

    os.makedirs(args.outdir, exist_ok=True)
    datasets = {
        "stores": stores, "products": products, "members": members,
        "sales": sales, "sale_items": items, "daily_store_summary": summary,
    }
    print("[gen] CSV 出力 ...")
    csv_paths = {}
    for t in TABLES_IN_LOAD_ORDER:
        p = os.path.join(args.outdir, f"{t}.csv")
        write_csv(p, t, datasets[t])
        csv_paths[t] = p

    print("[db ] 接続 ...")
    conn = connect(args)
    try:
        if args.recreate:
            print("[db ] スキーマ再作成（--recreate）...")
            recreate_schema(conn)
        else:
            print("[db ] 既存テーブルを TRUNCATE ...")
            truncate_all(conn)

        print("[db ] COPY 投入 ...")
        for t in TABLES_IN_LOAD_ORDER:
            copy_table(conn, t, csv_paths[t])
            print(f"[db ]   {t} 投入完了")

        reset_sequences(conn)

        print("\n[verify] 検証結果 --------------------------------------------")
        for name, val in verify(conn):
            print(f"  {name}: {val}")
        print("-------------------------------------------------------------")
    finally:
        conn.close()

    if not args.keep_csv:
        for p in csv_paths.values():
            os.remove(p)

    print("[done] 完了")


if __name__ == "__main__":
    main()
