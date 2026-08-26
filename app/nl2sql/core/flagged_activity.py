"""段階2・パターンA：フラグの立った実体が、指定期間に実績を持つかを確認する。

`issue_c015`。型1（結合欠落）と型2（フラグ）が同じマスタ表を指しているかどうかは
データ依存で、列の一覧だけを見る静的規則では判定できない
（`issue_c013` 1.5 の自己規律により、ラベルを見てから作った近似規則は採らない）。

かわりに `demo_readonly` への軽い照会を1本行い、事実を直接確かめる。
判定するのは「フラグが立った行が、対象期間中に実績を持つか」の1点だけ。

**SQLは生成しない。** ここで発行するクエリは固定の3種類（stores/members/products）で、
質問文からSQLを組み立てるわけではない（`issue_c013` 論点Aの前提を崩さない）。

`issue_c020`：型1（結合欠落）を立てる前に、対象の母集合（フラグが解消済みなら
その絞り込み後、未解消ならマスタ全体）の中に、対象期間で実績ゼロの実体が
実在するかを同じ考え方（近似規則ではなくデータ照会）で確かめる
（`has_unrepresented_entity`）。実在しなければ、型1を立てても出力の顔ぶれは
変わらない——問い自体が空である。パターンAの「フラグの立った行」照会とは
見ている集合が違う（片方は"フラグが立った行"、もう片方は"（絞り込み後の）
母集合全体"）ため、別関数として足した。

`issue_c024`：質問文が `stores.region` の値に絞り込んでいる場合（`AxisExtraction.
region_filter`）、`check_flagged_activity` / `has_unrepresented_entity` 双方の
照会にその絞り込みを反映できるようにした（`region_filter` 引数、既定 `None` で
無視・後方互換）。`region` の表記ゆれ（関西/近畿）は `DIAGNOSIS.md` が既定を
「統合する」としている事実に合わせ、絞り込み時も両方の値を対象にする
（`_REGION_SYNONYMS`、近似ではなく既知の事実のハードコード。`FLAG_COLUMN` 等
既存の辞書と同じ考え方）。`region` 以外の列への一般化はしていない
（`stores`にしか無い列のため）。

`issue_c028`：`has_unrepresented_entity` はマスタ実体（`GROUP_BY=MASTER_*`）に限り、
`GROUP_BY=TIME`（時間軸）は対象外だった。`has_unrepresented_time_unit` を追加し、
指定期間（省略時は全期間）に実績ゼロの**暦日**が実在するかを確かめる。**区切りの
粒度（日・月・年）を①から新たに受け取る必要はない**——`demo_sales` は運用期間
（2024-08-15〜2026-08-14）を通じて実績ゼロの暦日が1日も無いことを実測済みで、
暦日の網羅性は月・年どの粒度で区切っても実績ゼロの単位が生じないことを含意する
（包含関係。日次で網羅されていれば、それを含むどの月・年も必ず実績を持つ）。
実在すれば型1を立てる根拠になり、実在しなければ`resolved=True`にする——
`has_unrepresented_entity`と同じ考え方（近似規則ではなくデータ照会）。
"""
from __future__ import annotations

from .sql_executor import connect_demo

#: `region`の表記ゆれ（issue_c015より前から既知。DIAGNOSIS.mdが既定を「統合する」と
#: しているため、絞り込みでも両方の値を対象にする）
_REGION_SYNONYMS = {
    "関西": ("関西", "近畿"),
    "近畿": ("関西", "近畿"),
}


def _region_clause(region_filter: str | None, *, alias: str = "st") -> tuple[str, list]:
    """`region_filter`が指定されていれば、その値（表記ゆれを含む）へ絞り込む一句を返す。"""
    if not region_filter or region_filter == "NONE":
        return "", []
    values = _REGION_SYNONYMS.get(region_filter, (region_filter,))
    placeholders = ", ".join(["%s"] * len(values))
    return f"AND {alias}.region IN ({placeholders})", list(values)

_QUERIES = {
    "stores": """
        SELECT EXISTS (
            SELECT 1 FROM sales s
            JOIN stores st ON st.store_id = s.store_id
            WHERE st.close_date IS NOT NULL
            {period_clause}
            {region_clause}
        )
    """,
    "members": """
        SELECT EXISTS (
            SELECT 1 FROM sales s
            JOIN members m ON m.member_id = s.member_id
            WHERE m.is_active = false
            {period_clause}
        )
    """,
    "products": """
        SELECT EXISTS (
            SELECT 1 FROM sale_items si
            JOIN sales s ON s.sale_id = si.sale_id
            JOIN products p ON p.product_id = si.product_id
            WHERE p.is_discontinued = true
            {period_clause}
        )
    """,
}


def _period_clause(period: str) -> tuple[str, list]:
    if not period or period == "NONE":
        return "", []
    start, _, end = period.partition("..")
    return "AND s.sold_at >= %s AND s.sold_at < %s", [start, end]


def check_flagged_activity(table: str, period: str, *, region_filter: str | None = None) -> bool:
    """`table` のフラグが立った実体が、`period` に実績を持つかを返す。

    Args:
        table: `"stores"` / `"members"` / `"products"`
        period: `AxisExtraction.period` の値。`"NONE"` なら全期間を対象にする
        region_filter: `AxisExtraction.region_filter` の値（`issue_c024`）。
            `stores`にだけ意味を持つ（`region`列を持つのは`stores`のみ）。
            `None`/`"NONE"`、または`stores`以外の`table`では無視する

    Raises:
        KeyError: `table` が対象外の値
    """
    query = _QUERIES[table]
    period_clause, period_params = _period_clause(period)
    region_clause, region_params = (
        _region_clause(region_filter) if table == "stores" else ("", [])
    )
    connection = connect_demo()
    try:
        with connection.cursor() as cur:
            cur.execute(
                query.format(period_clause=period_clause, region_clause=region_clause),
                period_params + region_params,
            )
            (exists,) = cur.fetchone()
            return bool(exists)
    finally:
        connection.close()


# --- issue_c020：型1（結合欠落）を立てる根拠の確認 --------------------------

_ROSTER_QUERIES = {
    "stores": {
        "active_clause": "st.close_date IS NULL",
        "empty_check": """
            SELECT EXISTS (
                SELECT 1 FROM stores st
                WHERE {active_clause}
                {region_clause}
                AND NOT EXISTS (
                    SELECT 1 FROM sales s
                    WHERE s.store_id = st.store_id
                    {period_clause}
                )
            )
        """,
    },
    "members": {
        "active_clause": "m.is_active = true",
        "empty_check": """
            SELECT EXISTS (
                SELECT 1 FROM members m
                WHERE {active_clause}
                AND NOT EXISTS (
                    SELECT 1 FROM sales s
                    WHERE s.member_id = m.member_id
                    {period_clause}
                )
            )
        """,
    },
    "products": {
        "active_clause": "p.is_discontinued = false",
        "empty_check": """
            SELECT EXISTS (
                SELECT 1 FROM products p
                WHERE {active_clause}
                AND NOT EXISTS (
                    SELECT 1 FROM sale_items si
                    JOIN sales s ON s.sale_id = si.sale_id
                    WHERE si.product_id = p.product_id
                    {period_clause}
                )
            )
        """,
    },
}


def has_unrepresented_entity(
    table: str, *, active_only: bool, period: str, region_filter: str | None = None
) -> bool:
    """`table`の母集合の中に、`period`で実績ゼロの実体が実在するかを返す。

    真なら型1（結合欠落）を立てる根拠になる。偽なら、型1を立てても実績ゼロの
    行が並ぶ余地が無く、問い自体が空になる（`issue_c020`）。

    Args:
        table: `"stores"` / `"members"` / `"products"`
        active_only: 真なら、質問文が解消済みのフラグで絞り込んだ後の母集合
            （例: 現在営業中の店舗）だけを見る。偽ならマスタ全体を見る
        period: `AxisExtraction.period` の値。`"NONE"` なら全期間を対象にする
        region_filter: `AxisExtraction.region_filter` の値（`issue_c024`）。
            `stores`にだけ意味を持つ。`None`/`"NONE"`、または`stores`以外の
            `table`では無視する

    Raises:
        KeyError: `table` が対象外の値
    """
    spec = _ROSTER_QUERIES[table]
    active_clause = spec["active_clause"] if active_only else "TRUE"
    period_clause, period_params = _period_clause(period)
    region_clause, region_params = (
        _region_clause(region_filter) if table == "stores" else ("", [])
    )
    query = spec["empty_check"].format(
        active_clause=active_clause, period_clause=period_clause, region_clause=region_clause
    )
    connection = connect_demo()
    try:
        with connection.cursor() as cur:
            cur.execute(query, region_params + period_params)
            (exists,) = cur.fetchone()
            return bool(exists)
    finally:
        connection.close()


# --- issue_c028(a)：時間軸（GROUP_BY=TIME）の空集合 --------------------------

_TIME_UNIT_EMPTY_QUERY = """
    SELECT EXISTS (
        SELECT 1 FROM generate_series(
            date_trunc('day', COALESCE(%s::date, (SELECT MIN(sold_at) FROM sales))),
            date_trunc('day', COALESCE(%s::date - interval '1 day', (SELECT MAX(sold_at) FROM sales))),
            interval '1 day'
        ) AS d
        WHERE NOT EXISTS (
            SELECT 1 FROM sales s WHERE date_trunc('day', s.sold_at) = d
        )
    )
"""


def _period_bounds(period: str) -> tuple[str | None, str | None]:
    if not period or period == "NONE":
        return None, None
    start, _, end = period.partition("..")
    return start, end


def has_unrepresented_time_unit(period: str) -> bool:
    """指定期間（`"NONE"`なら全期間）に、実績ゼロの暦日が実在するかを返す。

    真なら型1（結合欠落、`GROUP_BY=TIME`）を立てる根拠になる。偽なら、期間内の
    どの暦日にも実績があり——どんな区切りの粒度（日・月・年）でグループ化しても
    実績ゼロの単位が現れる余地が無い（`issue_c028`）。
    """
    start, end = _period_bounds(period)
    connection = connect_demo()
    try:
        with connection.cursor() as cur:
            cur.execute(_TIME_UNIT_EMPTY_QUERY, [start, end])
            (exists,) = cur.fetchone()
            return bool(exists)
    finally:
        connection.close()
