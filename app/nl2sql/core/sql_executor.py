"""検査済みのSQLを、読み取り専用の条件下で実行する。

安全側の作りは3層ある。この層が担うのは2層目。

    1層目: sql_validator … 構文と参照先の静的検査
    2層目: ここ           … 読み取り専用トランザクション + 実行時間の上限
    3層目: DBロール       … demo_readonly（SELECT のみ、demo_sales のみ）

1層目と3層目が無くてもこの層だけで書き込みは止まる、という重ね方にしている。
検査をすり抜ける構文があっても、`SET TRANSACTION READ ONLY` の下では書き込めない。

エラーは SQLSTATE で分類して返す。呼び出し側の用途が2つあるため。

    - 評価ランナー: 失敗の内訳（構文なのか、存在しない列なのか）を集計したい
    - self_correct: エラー文をLLMへ返して書き直させたい
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from decouple import config

from .sql_validator import DEFAULT_MAX_ROWS, ValidationResult, validate

#: 1クエリあたりの実行時間の上限（ミリ秒）
DEFAULT_TIMEOUT_MS = 10_000

# SQLSTATE から呼び出し側が扱いやすい分類へ。
# 個別コードに無いものは先頭2桁のクラスで拾う。
_ERROR_KINDS = {
    "42601": "syntax",       # syntax_error
    "42P01": "undefined",    # undefined_table
    "42703": "undefined",    # undefined_column
    "42883": "undefined",    # undefined_function
    "42P02": "undefined",    # undefined_parameter
    "42702": "ambiguous",    # ambiguous_column（別名を付け忘れた JOIN で出る）
    "42725": "ambiguous",    # ambiguous_function
    "42804": "type",         # datatype_mismatch
    "42846": "type",         # cannot_coerce（型を跨いだキャスト）
    "42803": "grouping",     # grouping_error（GROUP BY の付け忘れ）
    "42501": "permission",   # insufficient_privilege
    "57014": "timeout",      # query_canceled（statement_timeout もこれ）
    "25006": "read_only",    # read_only_sql_transaction
    "54001": "too_complex",  # statement_too_complex
}
_ERROR_CLASSES = {
    "08": "connection",
    "53": "resource",        # disk full / out of memory など
}


@dataclass
class QueryResult:
    """1回の実行結果。

    Attributes:
        ok: 実行に成功したか
        sql: 実際に実行したSQL（LIMIT付与後）。検査で落ちた場合は空
        columns: 列名
        rows: 取得した行（DBの型のまま。正規化は評価側の責務）
        row_count: 取得できた行数
        truncated: 上限に達したため、まだ続きがあり得るか
        elapsed_ms: 実行に要した時間
        error: エラーメッセージ（呼び出し側がLLMへ返すことを想定）
        error_kind: エラーの分類。validation / syntax / undefined / permission /
            timeout / read_only / connection / other
    """

    ok: bool
    sql: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    error: str = ""
    error_kind: str = ""


def classify_error(sqlstate: str | None) -> str:
    """SQLSTATE を分類名へ変換する。"""
    if not sqlstate:
        return "other"
    if sqlstate in _ERROR_KINDS:
        return _ERROR_KINDS[sqlstate]
    return _ERROR_CLASSES.get(sqlstate[:2], "other")


def connect_demo(schema: str = "demo_sales"):
    """デモDBへ読み取り専用ロールで接続する。

    `search_path` を対象スキーマだけに絞る。修飾なしの表名がこのスキーマの中でしか
    解決されなくなるため、`public` 配下へ流れ込む経路が1つ減る。

    ユーザー名は `.env` に無ければ `demo_readonly` を既定にする。
    """
    import psycopg2

    return psycopg2.connect(
        dbname=config("DEMO_DB_NAME", default=config("DB_NAME", default="llm_pipe_lab")),
        user=config("DEMO_DB_USER", default="demo_readonly"),
        password=config("DEMO_DB_PASSWORD", default=""),
        host=config("DB_HOST", default="localhost"),
        port=config("DB_PORT", default="5432"),
        options=f"-c search_path={schema}",
    )


def allowed_tables_from(snapshot) -> set[str]:
    """SchemaSnapshot から、検査に渡すホワイトリストを作る。

    修飾あり（`demo_sales.sales`）と修飾なし（`sales`）の両方を入れる。
    LLM がどちらの書き方をしても通すため。修飾なしを含めることで
    `public.sales` のような別スキーマの同名表も名前上は通ってしまうが、
    実際の遮断はロールと search_path が受け持つ。
    """
    names: set[str] = set()
    for table in snapshot.tables:
        names.add(table.name.lower())
        names.add(table.qualified_name.lower())
    return names


def execute_sql(
    connection,
    sql: str,
    *,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> QueryResult:
    """検査を通さずにSQLを実行する。読み取り専用の担保はこの関数が行う。

    検査込みで使いたい場合は `SqlExecutor.run()` を使うこと。
    この関数を直接使うのは、検査層を介さずに2層目の効きを確かめたい場合に限る。

    トランザクションは必ず rollback で閉じる。commit は一切しない。
    """
    import psycopg2

    started = time.time()
    try:
        with connection.cursor() as cur:
            # トランザクションの最初の文であること。後続の文より先に出す必要がある
            cur.execute("SET TRANSACTION READ ONLY")
            # SET はパラメータを取らないため値を埋め込む。int 化してから使う
            cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
            cur.execute(sql)

            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall() if cur.description else []
    except psycopg2.Error as e:
        connection.rollback()
        return QueryResult(
            ok=False,
            sql=sql,
            elapsed_ms=int((time.time() - started) * 1000),
            error=str(e).strip(),
            error_kind=classify_error(getattr(e, "pgcode", None)),
        )

    connection.rollback()

    # 検査層が LIMIT を max_rows に丸めるため、ちょうど max_rows 件なら
    # 続きがある可能性がある。「切れたかもしれない」以上のことは言えない
    return QueryResult(
        ok=True,
        sql=sql,
        columns=columns,
        rows=list(rows),
        row_count=len(rows),
        truncated=len(rows) >= max_rows,
        elapsed_ms=int((time.time() - started) * 1000),
    )


class SqlExecutor:
    """検査と実行をひとまとめにした入口。

    接続は遅延生成し、以後は使い回す。接続が壊れた場合（connection 系のエラー）は
    破棄して次回に張り直す。評価ランナーが数十件を連続で回すため、
    毎回張り直すより接続を保つほうが素直。

    `connect` が返した接続はこのオブジェクトが所有する。`close()` するか
    コンテキストマネージャとして使うこと。
    """

    def __init__(
        self,
        *,
        connect=None,
        schema: str = "demo_sales",
        allowed_tables: set[str] | None = None,
        max_rows: int = DEFAULT_MAX_ROWS,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ):
        self._connect = connect or (lambda: connect_demo(schema))
        self.schema = schema
        self.allowed_tables = allowed_tables
        self.max_rows = max_rows
        self.timeout_ms = timeout_ms
        self._connection = None

    def __enter__(self) -> SqlExecutor:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    def _get_connection(self):
        if self._connection is None:
            self._connection = self._connect()
        return self._connection

    def validate(self, sql: str) -> ValidationResult:
        return validate(
            sql, allowed_tables=self.allowed_tables, max_rows=self.max_rows
        )

    def run(self, sql: str) -> QueryResult:
        """検査してから実行する。検査で落ちた場合はDBに触れない。"""
        checked = self.validate(sql)
        if not checked.ok:
            return QueryResult(
                ok=False,
                error=checked.reason or "検査に失敗しました",
                error_kind="validation",
            )

        try:
            connection = self._get_connection()
        except Exception as e:  # 接続そのものに失敗した場合
            return QueryResult(
                ok=False,
                sql=checked.sql,
                error=str(e).strip(),
                error_kind="connection",
            )

        result = execute_sql(
            connection,
            checked.sql,
            timeout_ms=self.timeout_ms,
            max_rows=self.max_rows,
        )

        # 接続が死んでいる場合は次回に張り直す
        if result.error_kind == "connection":
            self.close()

        return result
