"""生成された SQL を、実行する前に検査する。

LLM が書いた SQL をそのまま DB へ流さないための関門。ただしここは多層防御の
1層目にすぎず、**最終的な保証は DB 側の読み取り専用ロールが持つ**。
この層だけで安全と見なしてはいけない。

検査の順序:
    1. Markdown のコードフェンスを剥がす（LLM が ```sql で囲んで返すことが多い）
    2. コメントを除去する（`--` と `/* */`。ブロックコメントのネストに対応）
    3. 文字列リテラルを空にした写しを作り、そちらでキーワードを検査する
       （リテラル中の `DELETE` などを禁止語として誤検出しないため）
    4. 複文を拒否する
    5. 先頭が SELECT / WITH であることを確認する
    6. 禁止キーワード・危険な関数を検出する
    7. 参照テーブルを抽出し、ホワイトリストと照合する
    8. LIMIT を付与する（既にあり、上限を超えていれば下げる）

戻り値の `sql` は、コメントを除去し LIMIT を付けた「実行用の SQL」。
呼び出し側は `ok` が True のときだけ `sql` を実行すること。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: LIMIT を明示しなかった場合に付与する既定の上限
DEFAULT_MAX_ROWS = 1000

# 参照だけを許す前提なので、状態を変える構文を弾く。
#
# 上段は「単一の SELECT / WITH の中に実際に書けてしまい、害があるもの」。
#   - Postgres は CTE の中に INSERT / UPDATE / DELETE / MERGE を書ける
#     （`WITH x AS (INSERT ... RETURNING *) SELECT * FROM x`）
#   - `SELECT ... INTO` はテーブルを作る
#   - `SELECT ... FOR UPDATE` は行ロックを取る（UPDATE で拾える）
# 下段は保険。単一の SELECT には書けないが、検査の抜けに備えて併せて弾く。
#
# 列名として現れそうな語（comment / created など）は入れない。
# `\b` 境界で照合するため created_at や settlement は誤検出しないが、
# 単独で列名になり得る語を禁止語にすると、正当なクエリを落としてしまう。
_FORBIDDEN_KEYWORDS = (
    # 上段: 実害があるもの
    "INSERT", "UPDATE", "DELETE", "MERGE", "INTO",
    # 下段: 保険
    "DROP", "ALTER", "CREATE", "TRUNCATE",
    "GRANT", "REVOKE", "COPY",
    "CALL", "EXECUTE", "PREPARE",
    "VACUUM", "REINDEX", "CLUSTER", "REFRESH",
    "LOCK", "LISTEN", "NOTIFY",
    "SET", "RESET", "DISCARD",
)

# 参照専用でも実害を出せる関数。ファイル読み出しと、時間を握るものを止める。
_FORBIDDEN_FUNCTIONS = (
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export",
    "dblink", "dblink_exec",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_terminate_backend", "pg_cancel_backend",
    "set_config",
    "query_to_xml",
)

# FROM に書ける集合返却関数のうち、参照だけで害が無く、正当な用途があるもの。
#
# `FROM generate_series(...)` は「取引の無かった日も 0 で並べる」ための定番
# （カレンダースパイン）で、このプロジェクトが検出対象にしている型1
# （実績の無い実体を並べるか）の答えそのものを表す形である。
# 許可しないと、正しい答えを書いた SQL をこちらが弾いてしまう。
#
# **ここに無い関数を FROM に書いた場合は、テーブル名として扱われ
# ホワイトリスト照合で落ちる。** 未知のものは通さない側に倒してある。
_ALLOWED_FROM_FUNCTIONS = frozenset({
    "generate_series",
    "unnest",
})

# FROM 句のテーブル列挙が終わる位置を判定するためのキーワード
_TABLE_LIST_STOP = re.compile(
    r"\b(WHERE|GROUP|ORDER|HAVING|LIMIT|OFFSET|FETCH|UNION|INTERSECT|EXCEPT"
    r"|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|NATURAL|ON|USING|WINDOW)\b",
    re.IGNORECASE,
)

#: 表名の前に置ける修飾語。読み飛ばす
_TABLE_PREFIX = re.compile(r"^(LATERAL|ONLY)\s+", re.IGNORECASE)

# `(` の直前に置かれても関数呼び出しではない語。
# `IN (SELECT ...)` の中の FROM は本物の表参照なので、拾わないと照合が緩む
_NON_FUNCTION_WORDS = {
    "IN", "EXISTS", "ALL", "ANY", "SOME", "AND", "OR", "NOT",
    "ON", "USING", "WHERE", "HAVING", "SELECT", "FROM", "JOIN",
    "BY", "VALUES", "RETURNING", "AS", "UNION", "INTERSECT", "EXCEPT",
    "WITH", "CASE", "WHEN", "THEN", "ELSE", "LATERAL", "DISTINCT",
}

#: `schema.table` / `"quoted"` を1件だけ拾う
_TABLE_NAME = re.compile(
    r'((?:"[^"]+"|[A-Za-z_][\w$]*)(?:\.(?:"[^"]+"|[A-Za-z_][\w$]*))?)'
)

#: 末尾の LIMIT（後ろに OFFSET が続く形も許す）
_LIMIT_TAIL = re.compile(r"\bLIMIT\s+(\d+)\s*(?:OFFSET\s+\d+\s*)?$", re.IGNORECASE)
_LIMIT_ALL_TAIL = re.compile(r"\bLIMIT\s+ALL\s*(?:OFFSET\s+\d+\s*)?$", re.IGNORECASE)

_CODE_FENCE = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)
_DOLLAR_QUOTE = re.compile(r"\$[A-Za-z_]*\$")


@dataclass
class ValidationResult:
    """検査結果。

    Attributes:
        ok: 実行してよいか
        sql: 実行用に整形した SQL（ok が False のときは整形途中の値が入り得る）
        reason: 拒否した理由（ok が True なら None）
        tables: 抽出した参照テーブル名（小文字・引用符なし。CTE 名を除く）
    """

    ok: bool
    sql: str = ""
    reason: str | None = None
    tables: list[str] = field(default_factory=list)


def strip_code_fence(text: str) -> str:
    """```sql ... ``` で囲まれていれば中身だけを返す。"""
    if not text:
        return ""
    m = _CODE_FENCE.match(text)
    return m.group(1) if m else text


def _split_comments_and_literals(sql: str) -> tuple[str, str]:
    """コメントを除去した SQL と、さらに文字列リテラルを空にした写しを返す。

    1文字ずつ見る必要がある。`'--'` のようにリテラルの中にコメント開始記号が
    入っている場合や、逆にコメントの中にクォートが入っている場合を、
    正規表現だけでは切り分けられない。
    """
    plain: list[str] = []  # コメント除去のみ
    scan: list[str] = []   # コメント除去 + リテラルを空に
    i, n = 0, len(sql)

    while i < n:
        c = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # 行コメント: 改行までを捨てる（改行自体は残す）
        if c == "-" and nxt == "-":
            j = sql.find("\n", i)
            plain.append(" ")
            scan.append(" ")
            if j == -1:
                break
            i = j
            continue

        # ブロックコメント: Postgres はネストを許すので深さを数える
        if c == "/" and nxt == "*":
            depth, i = 1, i + 2
            while i < n and depth > 0:
                if sql[i] == "/" and i + 1 < n and sql[i + 1] == "*":
                    depth += 1
                    i += 2
                elif sql[i] == "*" and i + 1 < n and sql[i + 1] == "/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            plain.append(" ")
            scan.append(" ")
            continue

        # 文字列リテラル: '' は escape された ' なので閉じ扱いにしない
        if c == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            plain.append(sql[i:j + 1])
            scan.append("''")
            i = j + 1
            continue

        # 引用識別子: 中身は識別子なので、そのまま残す（テーブル名抽出で使う）
        if c == '"':
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            chunk = sql[i:j + 1]
            plain.append(chunk)
            scan.append(chunk)
            i = j + 1
            continue

        plain.append(c)
        scan.append(c)
        i += 1

    return "".join(plain), "".join(scan)


def sanitized_for_scan(sql: str) -> str:
    """コメントを除去し、文字列リテラルを空にした写しを返す。

    SQLの構文をざっと走査したい他のモジュール（`ORDER BY` の有無を見るなど）が、
    同じ走査を書き直さずに済むように公開している。実行には使わないこと。
    """
    _, scan = _split_comments_and_literals(strip_code_fence(sql or ""))
    return scan


def _mask_identifiers(scan: str) -> str:
    """引用識別子を無害な語に潰す。

    `"delete"` のような引用識別子を禁止語として誤検出しないため、
    キーワード検査にはこちらを使う。
    """
    return re.sub(r'"[^"]*"', "ident", scan)


def _count_statements(masked: str) -> int:
    return len([s for s in masked.split(";") if s.strip()])


def _extract_cte_names(masked: str) -> set[str]:
    """WITH 句で定義された名前を集める。

    CTE 名は物理テーブルではないので、ホワイトリスト照合の対象から外す。
    """
    if not re.match(r"^\s*\(*\s*WITH\b", masked, re.IGNORECASE):
        return set()
    return {
        m.group(1).lower()
        for m in re.finditer(r"\b([A-Za-z_][\w$]*)\s+AS\s*\(", masked, re.IGNORECASE)
    }


def _is_inside_function_call(scan: str, index: int) -> bool:
    """`index` の位置が関数呼び出しの括弧の中かを判定する。

    `EXTRACT(YEAR FROM x)` や `substring(s FROM 1)` の FROM は表参照ではない。
    直前の未閉じ括弧が識別子の直後に開かれていれば関数呼び出しとみなす。
    ただし `IN (SELECT ... FROM t)` のような語は関数ではないので除く
    （ここを取りこぼすと、副問い合わせの表が照合から漏れる）。
    """
    depth = 0
    i = index - 1
    while i >= 0:
        char = scan[i]
        if char == ")":
            depth += 1
        elif char == "(":
            if depth == 0:
                j = i - 1
                while j >= 0 and scan[j].isspace():
                    j -= 1
                end = j + 1
                while j >= 0 and (scan[j].isalnum() or scan[j] == "_"):
                    j -= 1
                word = scan[j + 1: end].upper()
                return bool(word) and word not in _NON_FUNCTION_WORDS
            depth -= 1
        i -= 1
    return False


def _split_table_list(scan: str, start: int) -> list[str]:
    """FROM / JOIN の直後から、表の列挙を括弧の深さを見ながら切り出す。

    深さ0のカンマだけを区切りとする。`FROM (SELECT a, b FROM t) x` の
    括弧内のカンマを区切りにすると、`b` を表名と誤認してしまう。
    """
    items: list[str] = []
    buffer: list[str] = []
    depth = 0
    i, n = start, len(scan)

    while i < n:
        char = scan[i]
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break  # 外側の括弧が閉じた＝列挙の終わり
            depth -= 1
        elif depth == 0:
            if char == ",":
                items.append("".join(buffer))
                buffer = []
                i += 1
                continue
            if _TABLE_LIST_STOP.match(scan, i):
                break
        buffer.append(char)
        i += 1

    items.append("".join(buffer))
    return items


def _extract_tables(scan: str) -> list[str]:
    """FROM / JOIN の後ろからテーブル名を拾う。

    サブクエリ（`(` で始まるもの）は飛ばす。中身の FROM は別途拾われる。
    別名は先頭トークンだけ見るので自然に落ちる。

    `FROM func(...)` の形は集合返却関数の呼び出しであって表参照ではない。
    `_ALLOWED_FROM_FUNCTIONS` にあるものだけ読み飛ばし、**それ以外は
    名前をそのまま返す**（呼び出し側のホワイトリスト照合で落ちる）。
    知らない関数を素通しするより、表名として落とすほうが安全側。
    """
    found: list[str] = []
    for m in re.finditer(r"\b(FROM|JOIN)\b", scan, re.IGNORECASE):
        if _is_inside_function_call(scan, m.start()):
            continue

        for item in _split_table_list(scan, m.end()):
            token = _TABLE_PREFIX.sub("", item.strip())
            if not token or token.startswith("("):
                continue
            name = _TABLE_NAME.match(token)
            if not name:
                continue
            bare = name.group(1).replace('"', "").lower()
            # 名前の直後が `(` なら関数呼び出し
            if token[name.end():].lstrip().startswith("("):
                if bare in _ALLOWED_FROM_FUNCTIONS:
                    continue
            found.append(bare)

    # 出現順を保ったまま重複を除く
    seen: set[str] = set()
    ordered: list[str] = []
    for name in found:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _apply_limit(body: str, max_rows: int) -> str:
    """末尾の LIMIT を上限に収める。無ければ付ける。

    サブクエリで包む方式は取らない。包むと ORDER BY の効きが変わり得るため、
    「上位N件」を問う質問の答えが壊れる。
    """
    m = _LIMIT_TAIL.search(body)
    if m:
        current = int(m.group(1))
        if current <= max_rows:
            return body
        return body[: m.start(1)] + str(max_rows) + body[m.end(1):]

    m_all = _LIMIT_ALL_TAIL.search(body)
    if m_all:
        # LIMIT ALL は上限なしの意味。丸ごと差し替える
        return body[: m_all.start()] + f"LIMIT {max_rows}"

    return f"{body} LIMIT {max_rows}"


def validate(
    sql: str,
    *,
    allowed_tables: set[str] | None = None,
    max_rows: int = DEFAULT_MAX_ROWS,
) -> ValidationResult:
    """SQL を検査し、実行用に整形して返す。

    Args:
        sql: LLM が生成した SQL（コードフェンス付きでもよい）
        allowed_tables: 参照を許すテーブル名の集合。`{"demo_sales.sales", "sales"}` の
            ようにスキーマ修飾あり／なしのどちらでも一致する。None なら照合しない
        max_rows: 付与する LIMIT の上限
    """
    raw = strip_code_fence(sql or "").strip()
    if not raw:
        return ValidationResult(ok=False, reason="SQLが空です")

    if _DOLLAR_QUOTE.search(raw):
        # ドル引用符の中身は解析していないため、通さない
        return ValidationResult(ok=False, sql=raw, reason="ドル引用符($$)を含むSQLは許可していません")

    plain, scan = _split_comments_and_literals(raw)
    masked = _mask_identifiers(scan)

    if _count_statements(masked) > 1:
        return ValidationResult(ok=False, sql=plain.strip(), reason="複数の文は実行できません")

    # `TABLE foo` は Postgres では `SELECT * FROM foo` と同義だが、FROM を持たないため
    # テーブル名を抽出できず、ホワイトリスト照合をすり抜ける。よって許可しない。
    head = masked.lstrip().lstrip("(").lstrip()
    first = re.match(r"[A-Za-z_]+", head)
    if not first or first.group(0).upper() not in ("SELECT", "WITH"):
        word = first.group(0).upper() if first else "(不明)"
        return ValidationResult(
            ok=False, sql=plain.strip(), reason=f"SELECT / WITH で始まらないSQLです（先頭: {word}）"
        )

    upper = masked.upper()
    for kw in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", upper):
            return ValidationResult(
                ok=False, sql=plain.strip(), reason=f"禁止されたキーワードを含みます: {kw}"
            )

    lowered = masked.lower()
    for fn in _FORBIDDEN_FUNCTIONS:
        if re.search(rf"\b{re.escape(fn)}\b", lowered):
            return ValidationResult(
                ok=False, sql=plain.strip(), reason=f"禁止された関数を含みます: {fn}"
            )

    if re.search(r"\bFETCH\s+(FIRST|NEXT)\b", upper):
        # LIMIT を後付けできない形。生成側で LIMIT に寄せる
        return ValidationResult(
            ok=False, sql=plain.strip(), reason="FETCH FIRST/NEXT には未対応です（LIMITを使ってください）"
        )

    cte_names = _extract_cte_names(masked)
    tables = [t for t in _extract_tables(scan) if t not in cte_names]

    if allowed_tables is not None:
        allowed = {t.lower() for t in allowed_tables}
        for table in tables:
            bare = table.split(".")[-1]
            if table not in allowed and bare not in allowed:
                return ValidationResult(
                    ok=False,
                    sql=plain.strip(),
                    reason=f"許可されていないテーブル／関数を参照しています: {table}",
                    tables=tables,
                )

    body = plain.strip().rstrip(";").strip()
    return ValidationResult(ok=True, sql=_apply_limit(body, max_rows), tables=tables)
