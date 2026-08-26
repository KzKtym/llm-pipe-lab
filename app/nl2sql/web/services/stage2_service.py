"""段階2（ルール判定版）の振る舞いを1枚で見せるページの組み立て。

既存3画面がバッチ実験を比べる器なのに対し、ここは**1問に対してどう振る舞うか**を
順に見せる。導入検討の場で画面を出しながら説明するためのもの（issue_v005）。

**`disclosure_text` には一切手を入れない。** 整形も要約も言い換えも分割もしない。
保存されている文字列をそのままテンプレートへ渡す。

理由は2つある。

1. 現在の開示文には列名（`stores.close_date` 等）が混入しているが、それは工程②の
   問題（issue_c018）で直すもの。**画面側で隠すと、直ったかどうかが画面から
   分からなくなる**
2. 開示と聞き返しは1本の文字列に入っており、改行で切れば2つに割れるように見える。
   だが改行位置に依存する処理は、issue_c018 で文面が変われば黙って壊れる
"""
from __future__ import annotations

from ...models import Nl2SqlStage2RuleExperiment
from ...stage2_rule_store import read_details

#: 1問の振る舞いの種類。**この3つを同じ件数ずつ**見せるのが既定
KIND_CLARIFICATION = "clarification"
KIND_DISCLOSED = "disclosed"
KIND_NO_ASSUMPTION = "no_assumption"

ALL_KINDS = (KIND_CLARIFICATION, KIND_DISCLOSED, KIND_NO_ASSUMPTION)

#: 種類ごとに見せる件数
PER_KIND = 2

KIND_LABELS = {
    KIND_CLARIFICATION: "聞き返しが必要",
    KIND_DISCLOSED: "前提を開示して回答",
    KIND_NO_ASSUMPTION: "補う前提なしで回答",
}


def kind_of(item: dict) -> str:
    """その問いがどう振る舞ったか。

    `strip()` は**空かどうかの判定にだけ**使う。表示する文字列には触らない。
    """
    if item.get("needs_clarification"):
        return KIND_CLARIFICATION
    if (item.get("disclosure_text") or "").strip():
        return KIND_DISCLOSED
    return KIND_NO_ASSUMPTION


def walkthrough_slice(items: list) -> list:
    """**種類ごとに先頭から `PER_KIND` 件ずつ**取る。並びは明細の順のまま。

    **見せる問いを番号で書き並べない。** 番号で選ぶと、評価データが変わるたびに
    画面側の都合で選び直すことになり、都合のよい問いを選んだ形になる。
    種類ごとに機械的に切ることで、選別が入る余地を無くしている。

    「先頭から3種が揃うまで」から差し替えた。あの規則だと**並び順という無関係な
    要因で内訳が大きくぶれる**（`exp_3` では既定7問のうち4問が聞き返しになり、
    冒頭4問が続けて聞き返しになっていた）。

    狙いは全体の比率を再現することではない。評価データは規則を色々な条件で
    試すための盲検セットで、実際にどの程度聞き返すかを代表する数字ではないため、
    比率を写すとそのセットの偶然の内訳を正当化することになる。
    **3つの挙動を漏れなく、同じ程度の件数で見せる**のが狙い。

    ある種類が `PER_KIND` に満たなければ、その分だけ少なくなる。足せないため。
    """
    counts = {kind: 0 for kind in ALL_KINDS}
    taken = []
    for item in items:
        kind = kind_of(item)
        if counts[kind] >= PER_KIND:
            continue
        counts[kind] += 1
        taken.append(item)
    return taken


def build_rows(items: list) -> list[dict]:
    """1問ずつの表示用データ。

    `disclosure_text` は `None` を空文字にするだけで、**中身は素通し**。
    """
    rows = []
    for item in items:
        text = item.get("disclosure_text") or ""
        kind = kind_of(item)
        rows.append({
            "id": item.get("id", ""),
            "question": item.get("question", ""),
            "disclosure_text": text,
            # 空かどうかの判定にだけ strip を使う（text 自体は加工しない）
            "has_disclosure": bool(text.strip()),
            "kind": kind,
            "kind_label": KIND_LABELS[kind],
        })
    return rows


def build_stage2_context(experiment_id: int | None = None, show_all: bool = False) -> dict | None:
    """段階2ページのコンテキスト。

    Args:
        experiment_id: `None` なら最新（モデルの `Meta.ordering` が `-id`）
        show_all: `True` で全件。`False` は3種が揃うまで

    Returns:
        `None` は「指定された実験が無い」（ビューが404にする）。
        実験が1件も無い場合は `experiment` が `None` のコンテキストを返す
        （空の画面を出す。404 にするのは指定を外したときだけ）
    """
    experiments = list(Nl2SqlStage2RuleExperiment.objects.all())

    if experiment_id is None:
        experiment = experiments[0] if experiments else None
    else:
        experiment = next((e for e in experiments if e.id == experiment_id), None)
        if experiment is None:
            return None

    payload = read_details(experiment.id) if experiment else None
    items = (payload or {}).get("items", [])
    shown = items if show_all else walkthrough_slice(items)

    return {
        "experiment": experiment,
        "experiments": experiments,
        "rows": build_rows(shown),
        "shown_count": len(shown),
        "total_count": len(items),
        "show_all": show_all,
        "details_missing": experiment is not None and payload is None,
    }
