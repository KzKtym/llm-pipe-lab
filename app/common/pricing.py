"""トークン数から概算コストを出す。

`llm_client` は単価を持たない（「単価は変動するため、この層はトークン数と
所要時間だけを返す」）。その約束を守ったまま金額を出すために、単価表をここへ置く。

**金額は保存しない。記録に残すのはトークン数だけで、表示のたびにここで導く。**
保存すると、単価が改定された時点で過去の記録が実態と食い違い、
しかも「いつの単価で計算した値か」が記録から読めなくなる。
トークン数は事実なので変わらない。

**知らないモデルは `None` を返す。** 近いモデルの単価で代用しない。
概算とはいえ、根拠のない数字を出すほうが害が大きい。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 単価表の基準日。改定を追えなくなるので必ず更新すること
RATES_AS_OF = "2026-08-23"


@dataclass(frozen=True)
class Rate:
    """100万トークンあたりの単価（USD）。"""

    input_per_1m: float
    output_per_1m: float


#: モデル名 → 単価。`provider:model` の provider は落として引く
_RATES: dict[str, Rate] = {
    "gpt-4.1": Rate(2.00, 8.00),
    "gpt-4.1-mini": Rate(0.40, 1.60),
    "gpt-4.1-nano": Rate(0.10, 0.40),
}


def _normalize(model: str) -> str:
    """`openai:gpt-4.1-mini` / `gpt-4.1-mini-2025-04-14` を表の見出しへ寄せる。"""
    name = (model or "").strip().lower()
    if ":" in name:
        name = name.partition(":")[2]
    if name in _RATES:
        return name
    # 日付サフィックス付き（`gpt-4.1-mini-2025-04-14`）は、最長一致で引く
    for key in sorted(_RATES, key=len, reverse=True):
        if name.startswith(key):
            return key
    return name


def known_models() -> list[str]:
    return sorted(_RATES)


def rate_for(model: str) -> Rate | None:
    """単価を返す。表に無ければ `None`。"""
    return _RATES.get(_normalize(model))


def estimate_cost_usd(
    model: str, prompt_tokens: int, completion_tokens: int
) -> float | None:
    """概算コスト（USD）。単価が分からなければ `None`。

    キャッシュ割引・バッチ割引は見ていない。**上限側の概算**として読むこと。
    """
    rate = rate_for(model)
    if rate is None:
        return None
    return (
        (prompt_tokens or 0) * rate.input_per_1m
        + (completion_tokens or 0) * rate.output_per_1m
    ) / 1_000_000


def format_cost_usd(cost: float | None) -> str:
    """画面に出す表記。`None` は「不明」と書く（0 と混同させない）。"""
    if cost is None:
        return "—"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"
