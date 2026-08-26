"""生成SQLの実行結果と、正解SQLの実行結果を突き合わせる。

実行結果一致率（execution accuracy）の判定を担う。SQLの文字列同士を比べても
書き方の違いで落ちるだけなので、**実際に流した結果**で比べる。

比較を素直な `==` にできない理由が3つある。

    - 型が揺れる … `Decimal('1200.00')` と `1200` と `1200.0` は同じ値
    - 列名が違う … `count(*)` と `count(*) AS cnt` は同じ答え
    - 順序が意味を持つ場合と持たない場合がある

3つ目は正解SQL側で判断する。**正解SQLの最上位に ORDER BY があれば順序も比較し、
無ければ行の多重集合として比較する。** 「多い順に」と聞かれている質問では
並びが答えの一部であり、そうでない質問では並びは答えではない。
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from .sql_validator import sanitized_for_scan

#: 数値を丸める桁数。浮動小数の誤差で落とさないため
DEFAULT_DIGITS = 6


@dataclass
class ComparisonResult:
    match: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.match


def normalize_value(value, digits: int = DEFAULT_DIGITS):
    """比較できる形に値をならす。

    - 数値は Decimal に寄せて丸める（`1200` と `Decimal('1200.00')` を同じ扱いにする）
    - 日付・時刻は ISO 文字列
    - 文字列は末尾の空白のみ落とす（`char(n)` のパディング対策。先頭は意味があり得るので残す）
    - bool は数値に混ぜない（Python では `True == 1` になってしまうため先に判定する）
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # 文字列にして数値から切り離す。Python では bool が int の派生で
        # `True == 1` かつ `True == Decimal(1)` になるため、そのまま返すと
        # 真偽値の列と 1/0 の列が同じ答え扱いになってしまう
        return "true" if value else "false"
    if isinstance(value, (int, float, Decimal)):
        try:
            quantized = Decimal(str(value)).quantize(Decimal(1).scaleb(-digits))
        except (InvalidOperation, ValueError):
            # 桁が大きすぎて丸められない場合は、そのままの値で比べる
            return Decimal(str(value))
        normalized = quantized.normalize()
        # Decimal('-0') と Decimal('0') を同じにする
        return Decimal(0) if normalized == 0 else normalized
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, str):
        return value.rstrip()
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return str(value)


def normalize_rows(rows, digits: int = DEFAULT_DIGITS) -> list[tuple]:
    return [tuple(normalize_value(v, digits) for v in row) for row in rows]


def has_top_level_order_by(sql: str) -> bool:
    """最上位に ORDER BY があるかを判定する。

    括弧の深さを数え、深さ0の `ORDER BY` だけを見る。
    ウィンドウ関数の `OVER (ORDER BY ...)`、サブクエリやCTEの中の
    `ORDER BY` は最上位ではないので数えない。
    """
    scan = sanitized_for_scan(sql)
    depth = 0
    index = 0
    length = len(scan)
    upper = scan.upper()

    while index < length:
        char = scan[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and upper.startswith("ORDER", index):
            before_ok = index == 0 or not (scan[index - 1].isalnum() or scan[index - 1] == "_")
            if before_ok:
                rest = upper[index + len("ORDER"):]
                # ORDER と BY の間には必ず空白が要る。これが無いと ORDERBY を拾う
                if rest[:1].isspace():
                    stripped = rest.lstrip()
                    if stripped.startswith("BY") and (
                        len(stripped) == 2
                        or not (stripped[2].isalnum() or stripped[2] == "_")
                    ):
                        return True
        index += 1

    return False


def compare_rows(
    gold_rows,
    actual_rows,
    *,
    ordered: bool,
    digits: int = DEFAULT_DIGITS,
) -> ComparisonResult:
    """行同士を突き合わせる。

    Args:
        ordered: True なら並び順も一致していること。False なら行の多重集合として比較する
    """
    gold = normalize_rows(gold_rows, digits)
    actual = normalize_rows(actual_rows, digits)

    if not gold and not actual:
        return ComparisonResult(True)

    gold_width = len(gold[0]) if gold else 0
    actual_width = len(actual[0]) if actual else 0
    if gold and actual and gold_width != actual_width:
        return ComparisonResult(
            False, f"列数が違う（正解 {gold_width} 列 / 生成 {actual_width} 列）"
        )

    if len(gold) != len(actual):
        return ComparisonResult(
            False, f"行数が違う（正解 {len(gold)} 行 / 生成 {len(actual)} 行）"
        )

    if ordered:
        if gold == actual:
            return ComparisonResult(True)
        if Counter(gold) == Counter(actual):
            return ComparisonResult(False, "行の並び順が違う")
        return ComparisonResult(False, "値が違う")

    if Counter(gold) == Counter(actual):
        return ComparisonResult(True)
    return ComparisonResult(False, "値が違う")


def compare_query_results(
    gold,
    actual,
    *,
    gold_sql: str,
    ordered: bool | None = None,
    digits: int = DEFAULT_DIGITS,
) -> ComparisonResult:
    """`QueryResult` 同士を突き合わせる。

    Args:
        ordered: 順序を見るか。**呼び出し側が明示するのが正しい使い方。**
            None を渡した場合のみ `gold_sql` の最上位 ORDER BY から推測するが、
            正解SQLの ORDER BY は「見やすさのため」に書かれていることが多く、
            質問が順序を要求しているかとは一致しない。推測に頼らないこと

    どちらかの実行が失敗している場合は不一致とする。
    """
    if not gold.ok:
        return ComparisonResult(False, "正解SQLの実行に失敗している")
    if not actual.ok:
        return ComparisonResult(False, "生成SQLの実行に失敗している")

    if ordered is None:
        ordered = has_top_level_order_by(gold_sql)

    return compare_rows(gold.rows, actual.rows, ordered=ordered, digits=digits)
