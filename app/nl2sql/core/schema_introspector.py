"""DBのスキーマ情報を読み取り、LLMに渡すテキストへ整形する。

NL→SQL の精度は、質問文よりも「スキーマをどう説明したか」で決まる部分が大きい。
そのため、取得（introspect）と整形（render_schema_text）を分けてある。
どこまで説明を載せるかは実験パラメータ（schema_desc / schema_mode）として振りたいので、
整形側だけを差し替えられるようにしている。

コメントの取得に `pg_catalog` を使う。`information_schema` にはカラムコメントが
無いため、両方を混ぜるより `pg_catalog` に寄せたほうが問い合わせが少なくて済む。

接続は外から受け取る。`.cursor()` を持つオブジェクトであれば何でもよく、
psycopg2 の接続でも Django の `connection` でもそのまま渡せる。
接続の生成と読み取り専用トランザクションの管理は sql_executor 側の責務とする。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 既定で対象にするリレーション種別（テーブル / パーティション親 / ビュー / マテビュー）
DEFAULT_RELKINDS = ("r", "p", "v", "m")

_RELKIND_LABEL = {
    "r": "TABLE",
    "p": "TABLE",
    "v": "VIEW",
    "m": "MATERIALIZED VIEW",
    "f": "FOREIGN TABLE",
}

_WHITESPACE = re.compile(r"\s+")


def _one_line(text: str | None) -> str:
    """コメントを1行に潰す。

    行末コメント（`-- ...`）として出力するため、改行が残っていると
    後続のカラム定義がコメントに飲まれる。
    """
    if not text:
        return ""
    return _WHITESPACE.sub(" ", text).strip()


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    not_null: bool = False
    comment: str = ""


@dataclass
class ForeignKey:
    """1つの外部キー制約。複合キーもあるため列はリストで持つ。"""

    columns: list[str]
    ref_schema: str
    ref_table: str
    ref_columns: list[str]


@dataclass
class TableInfo:
    name: str
    schema: str
    relkind: str = "r"
    comment: str = ""
    columns: list[ColumnInfo] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class SchemaSnapshot:
    """あるスキーマの構造を1時点で写したもの。

    テーブルは名前順で保持する。プロンプトに載る順序が実行ごとに変わると
    同じパラメータでも結果が揺れるため、順序は決定的にしておく。
    """

    schema: str
    tables: list[TableInfo] = field(default_factory=list)

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]

    def get(self, name: str) -> TableInfo | None:
        for table in self.tables:
            if table.name == name:
                return table
        return None


# --- 取得 -------------------------------------------------------------------

_SQL_TABLES = """
SELECT c.relname, c.relkind, obj_description(c.oid, 'pg_class')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relkind::text = ANY(%s)
ORDER BY c.relname
"""

_SQL_COLUMNS = """
SELECT c.relname,
       a.attname,
       format_type(a.atttypid, a.atttypmod),
       a.attnotnull,
       col_description(c.oid, a.attnum)
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s
  AND c.relkind::text = ANY(%s)
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY c.relname, a.attnum
"""

# conkey は列番号の配列。WITH ORDINALITY で並び順を保つ
# （複合主キーの列順が崩れると PRIMARY KEY の意味が変わる）
_SQL_PRIMARY_KEYS = """
SELECT c.relname, a.attname
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE n.nspname = %s AND con.contype = 'p'
ORDER BY c.relname, k.ord
"""

_SQL_FOREIGN_KEYS = """
SELECT c.relname       AS src_table,
       con.conname     AS constraint_name,
       a.attname       AS src_column,
       fn.nspname      AS ref_schema,
       fc.relname      AS ref_table,
       fa.attname      AS ref_column
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_class fc ON fc.oid = con.confrelid
JOIN pg_namespace fn ON fn.oid = fc.relnamespace
CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
JOIN pg_attribute fa ON fa.attrelid = fc.oid AND fa.attnum = con.confkey[k.ord::int]
WHERE n.nspname = %s AND con.contype = 'f'
ORDER BY c.relname, con.conname, k.ord
"""


def introspect(
    connection,
    schema: str,
    *,
    relkinds: tuple[str, ...] = DEFAULT_RELKINDS,
) -> SchemaSnapshot:
    """スキーマの構造を読み取る。

    Args:
        connection: `.cursor()` を持つDB接続（psycopg2 / Django のいずれでも可）
        schema: 対象スキーマ名
        relkinds: 対象にするリレーション種別

    Returns:
        テーブル名順の SchemaSnapshot。該当が無ければ tables は空
    """
    kinds = list(relkinds)
    tables: dict[str, TableInfo] = {}

    with connection.cursor() as cur:
        cur.execute(_SQL_TABLES, [schema, kinds])
        for name, relkind, comment in cur.fetchall():
            tables[name] = TableInfo(
                name=name,
                schema=schema,
                relkind=relkind,
                comment=_one_line(comment),
            )

        if not tables:
            return SchemaSnapshot(schema=schema, tables=[])

        cur.execute(_SQL_COLUMNS, [schema, kinds])
        for table_name, column, data_type, not_null, comment in cur.fetchall():
            table = tables.get(table_name)
            if table is None:
                continue
            table.columns.append(
                ColumnInfo(
                    name=column,
                    data_type=data_type,
                    not_null=bool(not_null),
                    comment=_one_line(comment),
                )
            )

        cur.execute(_SQL_PRIMARY_KEYS, [schema])
        for table_name, column in cur.fetchall():
            table = tables.get(table_name)
            if table is not None:
                table.primary_key.append(column)

        cur.execute(_SQL_FOREIGN_KEYS, [schema])
        # 複合外部キーは制約名でまとまるため、制約単位で束ね直す
        grouped: dict[tuple[str, str], ForeignKey] = {}
        for src_table, con_name, src_col, ref_schema, ref_table, ref_col in cur.fetchall():
            if src_table not in tables:
                continue
            key = (src_table, con_name)
            fk = grouped.get(key)
            if fk is None:
                fk = ForeignKey(
                    columns=[],
                    ref_schema=ref_schema,
                    ref_table=ref_table,
                    ref_columns=[],
                )
                grouped[key] = fk
                tables[src_table].foreign_keys.append(fk)
            fk.columns.append(src_col)
            fk.ref_columns.append(ref_col)

    return SchemaSnapshot(schema=schema, tables=[tables[k] for k in sorted(tables)])


# --- 整形 -------------------------------------------------------------------


def render_schema_text(
    snapshot: SchemaSnapshot,
    *,
    include_comments: bool = True,
    tables: list[str] | None = None,
    value_hints: dict[str, dict[str, list[str]]] | None = None,
) -> str:
    """LLMに渡すスキーマ説明を組み立てる。

    CREATE TABLE 風に書くのは、LLMがこの形を最も見慣れているため。
    ただし**これは実行可能なDDLではない**（ビューを列一覧で表す、既定値を省くなど）。
    あくまで説明用のテキストである。

    Args:
        snapshot: introspect の結果
        include_comments: 日本語コメントを載せるか（schema_desc パラメータ用）
        tables: 載せるテーブルを絞る場合の名前リスト（schema_mode: retrieved 用）。
            並び順は snapshot の順序（名前順）に従う
        value_hints: `{テーブル名: {カラム名: [値, ...]}}`。
            カテゴリ値の候補を添える（value_hint パラメータ用）。
            収集そのものは未実装で、呼び出し側から渡す口だけ用意している

    Returns:
        テーブルごとのブロックを空行で連結した文字列
    """
    selected = snapshot.tables
    if tables is not None:
        wanted = set(tables)
        selected = [t for t in snapshot.tables if t.name in wanted]

    blocks = [
        _render_table(t, include_comments, (value_hints or {}).get(t.name, {}))
        for t in selected
    ]
    return "\n\n".join(blocks)


def _render_table(
    table: TableInfo,
    include_comments: bool,
    value_hints: dict[str, list[str]],
) -> str:
    lines: list[str] = []

    if include_comments and table.comment:
        lines.append(f"-- {table.comment}")

    keyword = _RELKIND_LABEL.get(table.relkind, "TABLE")
    lines.append(f"CREATE {keyword} {table.qualified_name} (")

    body: list[str] = []
    for col in table.columns:
        definition = f"  {col.name} {col.data_type}"
        if col.not_null:
            definition += " NOT NULL"
        body.append((definition, _column_note(col, include_comments, value_hints)))

    if table.primary_key:
        body.append((f"  PRIMARY KEY ({', '.join(table.primary_key)})", ""))

    for fk in table.foreign_keys:
        ref = f"{fk.ref_schema}.{fk.ref_table}"
        body.append(
            (
                f"  FOREIGN KEY ({', '.join(fk.columns)}) "
                f"REFERENCES {ref} ({', '.join(fk.ref_columns)})",
                "",
            )
        )

    for index, (definition, note) in enumerate(body):
        # 最終行以外はカンマ。コメントはカンマの後ろに置く
        text = definition + ("," if index < len(body) - 1 else "")
        if note:
            text += f" -- {note}"
        lines.append(text)

    lines.append(");")
    return "\n".join(lines)


def _column_note(
    col: ColumnInfo,
    include_comments: bool,
    value_hints: dict[str, list[str]],
) -> str:
    """行末コメントを組み立てる。コメントと値候補の両方が有れば併記する。"""
    parts: list[str] = []
    if include_comments and col.comment:
        parts.append(col.comment)
    hints = value_hints.get(col.name)
    if hints:
        parts.append("値の例: " + ", ".join(hints))
    return " / ".join(parts)
