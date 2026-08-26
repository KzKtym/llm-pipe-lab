"""段階2・工程③：構造（`AxisExtraction`）から未確定な軸を判定する。純コード。LLMは呼ばない。

`issue_c013` 1.3（型1の規則）・「基準の確定」節（ロスターの線引き）、
`sample/sales/DIAGNOSIS.md`「既定ポリシー」（型2〜4）をそのままコードにしたもの。
この関数自体は `population_labels.json` を一度も参照せずに書いた
（`issue_c014` の盲検制約）。

    型1 結合欠落     … 既定を置かない。常に聞き返す
    型2 フラグ       … 既定を置き、開示する（実体を数える→現役のみ／事象を集計→全部含める）
    型3 同義値       … 既定は統合。開示する
    型4 包括値・NULL … 既定は除外せず独立区分。開示する

`issue_c015`：`issue_c014` の盲検測定で見つかった判定側の穴（パターンB・A）を直した。

- **パターンB**: `GROUP_BY_GRANULARITY` が `CLASSIFICATION`（分類列でのグループ化）
  なら型1にしない。値の種類があらかじめ少数に決まっており、行の欠落で分類自体が
  丸ごと消えることが無いため。`LIMITED`（トップN形式）も型1にしない
  ——ランキングの外に出るだけの実体は、そもそも出力に現れないかどうかが
  結果を左右しない
- **パターンA**: 型1と型2が同じマスタ表を指すとき、**近似規則ではなく
  `flagged_activity_checker`（`issue_c015` で追加した依存性注入）による
  データ照会で、両者が同一集合かどうかを直接確かめる。** 同一なら型2を
  型1へ吸収し、別立てしない

`issue_c020`：型1（マスタ実体、`GROUP_BY=TIME` は対象外）を立てる前に、
`roster_emptiness_checker`（依存性注入）で「対象の母集合（フラグが解消済みなら
その絞り込み後、未解消ならマスタ全体）に、対象期間で実績ゼロの実体が実在するか」を
データ照会で確かめる。実在しなければ型1を立てても出力の顔ぶれが変わらない
（問いが空）ため、`resolved=True`にする。パターンAと同じく近似規則は採らない。

`issue_c024`：質問文が`stores.region`の値に絞り込んでいる（`extraction.
region_filter`）場合、`flagged_activity_checker`/`roster_emptiness_checker`双方に
その絞り込みを渡す。あわせて、型1と同じ表でなくても（パターンAの対象外でも）、
絞り込み後の集合にフラグの立った実体が実在しなければそのフラグ軸を`resolved`に
する経路を独立に追加した——パターンAが「型1と型2が同じ表」のときだけ吸収判定を
行うのに対し、こちらは型2が単独で残る場合（型1が立たない・別の表を指す等）も含めて
効く。`region`以外の列への一般化はしていない。

`issue_c028`：`issue_c020`が意図的に絞っていた2点を広げた。

- **(a) 時間軸（`GROUP_BY=TIME`）**: それまで対象外だった。`time_emptiness_checker`
  （新しい依存性注入。実体版の`roster_emptiness_checker`と対になる）で、対象期間に
  実績ゼロの暦日が実在するかを確かめる。実在しなければ`resolved=True`にする——
  マスタ実体と同じ考え方（近似規則ではなくデータ照会）
- **(b) 母数（`GROUP_BY`の軸ではないマスタ実体）**: 「会員1人あたりの平均購入金額」
  のように、平均・比率の分母がマスタ実体の個数になっている場合、`GROUP_BY`自体が
  そのマスタ表を指していなくても（分類列でのグループ化や単一値の場合を含む）型1の
  対象にする。①に`RATE_DENOMINATOR`項目を追加し、`GROUP_BY`の代わりにこちらが
  マスタ表を指していれば同じ経路（`roster_emptiness_checker`によるデータ照会）で
  判定する
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 型2：マスタ表ごとのフラグ列（表示用）
FLAG_COLUMN = {
    "stores": "close_date",
    "members": "is_active",
    "products": "is_discontinued",
}

#: 型1の対象になりうる GROUP_BY 種別（マスタ軸）と表示名
_MASTER_GROUP_BY_NAME = {
    "MASTER_STORES": "店舗の母集合（結合欠落）",
    "MASTER_MEMBERS": "会員の母集合（結合欠落）",
    "MASTER_PRODUCTS": "商品の母集合（結合欠落）",
}
#: GROUP_BY の値 → 対応するマスタ表名（`FLAG_COLUMN` / 照会関数のキーと揃える）
_MASTER_GROUP_BY_TABLE = {
    "MASTER_STORES": "stores",
    "MASTER_MEMBERS": "members",
    "MASTER_PRODUCTS": "products",
}
_TIME_AXIS_NAME = "時間軸の母集合（暗黙のカレンダー）"


@dataclass
class RuleAxis:
    """1つの未確定（または確定済み）候補軸。"""

    name: str
    #: 1=結合欠落 / 2=フラグ / 3=同義値 / 4=包括値・NULL
    kind: int
    resolved: bool
    #: kind が 2〜4 かつ unresolved のときだけ埋まる（適用した既定の説明）
    default: str = ""


@dataclass
class RuleResult:
    """判定結果。`axes` には確定済みのものも含める（監査用）。"""

    axes: list[RuleAxis] = field(default_factory=list)

    @property
    def needs_clarification(self) -> bool:
        """型1が1つでも未確定なら聞き返す。型1に既定は無い。"""
        return any(a.kind == 1 and not a.resolved for a in self.axes)

    @property
    def defaults_applied(self) -> list[RuleAxis]:
        """型2〜4のうち、既定を適用したもの（開示の対象）。"""
        return [a for a in self.axes if a.kind in (2, 3, 4) and not a.resolved]

    @property
    def unresolved_axes(self) -> list[RuleAxis]:
        return [a for a in self.axes if not a.resolved]


def classify(
    extraction,
    *,
    flagged_activity_checker=None,
    roster_emptiness_checker=None,
    time_emptiness_checker=None,
) -> RuleResult:
    """`AxisExtraction` から `RuleResult` を組み立てる。

    Args:
        extraction: `axis_extractor.AxisExtraction`（ダックタイピングで属性だけ見る）
        flagged_activity_checker: `(table: str, period: str, *, region_filter: str | None) -> bool`
            を実装した callable。「`table` のフラグが立った実体が、`period`
            （・`region_filter`が指定されていればその絞り込み後）に実績を持つか」を返す
            （`issue_c015` パターンA、`region_filter`は`issue_c024`）。`None` なら
            吸収判定を行わず、型1・型2を常に別々に検出する（旧・`issue_c014` 時点の
            挙動と同じ、フォールバック）。本番では
            `core.flagged_activity.check_flagged_activity` を渡す
        roster_emptiness_checker: `(table: str, *, active_only: bool, period: str,
            region_filter: str | None) -> bool` を実装した callable。「`table`の母集合
            （`active_only`なら解消済みフラグでの絞り込み後、`region_filter`が指定
            されていればさらにその絞り込み後）に、`period`で実績ゼロの実体が
            実在するか」を返す（`issue_c020`、`region_filter`は`issue_c024`）。
            `None` なら常に型1を立てる（`issue_c015`までの挙動と同じ、フォールバック）。
            本番では `core.flagged_activity.has_unrepresented_entity` を渡す
        time_emptiness_checker: `(period: str) -> bool` を実装した callable。
            「対象期間（`"NONE"`なら全期間）に、実績ゼロの暦日が実在するか」を返す
            （`issue_c028` (a)。`roster_emptiness_checker` の時間軸版）。`None` なら
            常に型1を立てる（フォールバック）。本番では
            `core.flagged_activity.has_unrepresented_time_unit` を渡す
    """
    axes: list[RuleAxis] = []

    # 型1：結合欠落。
    # - マスタ軸（GROUP_BY_GRANULARITY=IDENTITY）で、かつトップN形式でないときだけ成立
    #   （issue_c015 パターンB：分類列でのグループアップ、トップN形式は対象外）
    # - issue_c028 (b)：GROUP_BY自体がそのマスタ表を指していなくても（例:
    #   「会員ランク別に、会員1人あたりの平均購入金額」は GROUP_BY=MASTER_MEMBERS＋
    #   CLASSIFICATION、または GROUP_BY=NONE）、RATE_DENOMINATOR（平均・比率の
    #   分母）がマスタ実体を指していれば同じ扱いにする
    # - 時間軸（GROUP_BY=TIME）も対象。開いた期間別集計のみで、固定時点の比較は
    #   抽出側（①）で GROUP_BY=TIME にならない（issue_c015 パターンG）
    type1_table = None
    type1_axis_key = None
    if extraction.aggregation == "EVENT":
        if (
            extraction.group_by in _MASTER_GROUP_BY_NAME
            and extraction.group_by_granularity == "IDENTITY"
            and extraction.limited != "YES"
        ):
            type1_axis_key = extraction.group_by
        elif (
            getattr(extraction, "rate_denominator", "NONE") in _MASTER_GROUP_BY_NAME
            and extraction.limited != "YES"
        ):
            type1_axis_key = extraction.rate_denominator

        if type1_axis_key is not None:
            type1_table = _MASTER_GROUP_BY_TABLE[type1_axis_key]
            type1_resolved = extraction.group_by_resolved == "RESOLVED"

            # issue_c020：質問文だけでは未確定でも、対象の母集合（解消済みフラグが
            # あればその絞り込み後）に実績ゼロの実体がそもそも実在しないなら、
            # 型1を立てても出力の顔ぶれは変わらない（問いが空）
            if not type1_resolved and roster_emptiness_checker is not None:
                flag_state = {
                    "stores": extraction.flag_stores,
                    "members": extraction.flag_members,
                    "products": extraction.flag_products,
                }[type1_table]
                active_only = flag_state == "RESOLVED"
                if not roster_emptiness_checker(
                    type1_table,
                    active_only=active_only,
                    period=extraction.period,
                    region_filter=extraction.region_filter,
                ):
                    type1_resolved = True

            axes.append(
                RuleAxis(
                    name=_MASTER_GROUP_BY_NAME[type1_axis_key],
                    kind=1,
                    resolved=type1_resolved,
                )
            )
        elif extraction.group_by == "TIME":
            type1_resolved = extraction.group_by_resolved == "RESOLVED"

            # issue_c028 (a)：質問文だけでは未確定でも、対象期間に実績ゼロの暦日が
            # そもそも実在しないなら、型1を立てても出力の顔ぶれは変わらない
            if not type1_resolved and time_emptiness_checker is not None:
                if not time_emptiness_checker(extraction.period):
                    type1_resolved = True

            axes.append(
                RuleAxis(
                    name=_TIME_AXIS_NAME,
                    kind=1,
                    resolved=type1_resolved,
                )
            )

    # 型2：フラグ。既定の向きは「数える対象が実体か事象か」で決まる
    # （DIAGNOSIS.md 既定ポリシー・型2の切り分け）。
    default_direction = "現役のみ" if extraction.aggregation == "ENTITY" else "全部含める"
    for table, flag_state in (
        ("stores", extraction.flag_stores),
        ("members", extraction.flag_members),
        ("products", extraction.flag_products),
    ):
        if flag_state == "NONE":
            continue
        resolved = flag_state == "RESOLVED"

        # パターンA：型1と同じマスタ表を指しているとき、データ照会で吸収判定する。
        if not resolved and table == type1_table and flagged_activity_checker is not None:
            if not flagged_activity_checker(
                table, extraction.period, region_filter=extraction.region_filter
            ):
                # フラグが立った実体はこの期間に実績が無い＝型1が指す集合と同一。
                # 型2として別立てしない（型1の解消がそのままこちらも解消する）
                continue

        # issue_c024：型1と同じ表でなくても（＝上のパターンAが対象にしなくても）、
        # 質問文がその表を絞り込んでいる（region_filter）なら、絞り込み後の集合に
        # フラグの立った実体がそもそも実在しないかを確かめる。無ければこのフラグの
        # 問い自体が空である——issue_c020がactive_onlyを足したのと同じ型の穴
        if (
            not resolved
            and table != type1_table
            and table == "stores"
            and extraction.region_filter != "NONE"
            and flagged_activity_checker is not None
        ):
            if not flagged_activity_checker(
                table, extraction.period, region_filter=extraction.region_filter
            ):
                resolved = True

        axes.append(
            RuleAxis(
                name=f"{table}.{FLAG_COLUMN[table]}",
                kind=2,
                resolved=resolved,
                default="" if resolved else default_direction,
            )
        )

    # 型3：同義値。既定は統合。
    if extraction.synonym != "NONE":
        column, _, state = extraction.synonym.partition("_")
        resolved = state == "RESOLVED"
        axes.append(
            RuleAxis(
                name=f"{column}の表記ゆれ",
                kind=3,
                resolved=resolved,
                default="" if resolved else "統合する",
            )
        )

    # 型4：包括値・NULL。既定は除外せず独立区分として表示。
    if extraction.catchall != "NONE":
        column, _, state = extraction.catchall.partition("_")
        resolved = state == "RESOLVED"
        axes.append(
            RuleAxis(
                name=f"{column}の包括値",
                kind=4,
                resolved=resolved,
                default="" if resolved else "除外せず独立区分として表示",
            )
        )

    return RuleResult(axes=axes)
