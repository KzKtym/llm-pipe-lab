"""一覧画面の組み立て。

一覧で要るのは「どの設定で測ったか」「見てはいけない数字か」「明細があるか」の3点。
明細の有無は、比較の相手を選ぶ時点で分かる必要がある。無い実験を選ぶと
問題単位の勝敗が出せず、比較画面の主目的が空振りするため。
"""
from __future__ import annotations

from ...models import Nl2SqlExperiment
from ...params import Nl2SqlParams
from ...store import details_path

#: パラメータ比較で「そのキーの記録が無い」ことを表す番兵
_MISSING = object()
#: 記録が無いキーの表示
UNRECORDED = "—"


def parameter_line(parameters: dict | None) -> str:
    """パラメータの1行表記。

    `Nl2SqlParams` を通すのは、記録に無いキーの既定値も含めて同じ書式で並べるため。
    ただし**整形の失敗で一覧を落とさない**。パラメータは実験ごとに書き換わり得る
    JSON なので、後から仕様が変わった記録が混じることを前提にする。
    """
    try:
        return Nl2SqlParams.from_dict(parameters or {}).summary_line()
    except (ValueError, TypeError):
        return ", ".join(f"{key}: {value}" for key, value in sorted((parameters or {}).items()))


def sort_parameter_keys(keys) -> list[str]:
    """パラメータのキーを `Nl2SqlParams` の定義順に並べる。

    定義順はスキーマ→例示→生成→再試行→実行という関心の順になっている。
    アルファベット順より追いやすいので、詳細・比較の表はこの順で出す。
    定義に無いキー（仕様変更前の記録など）は後ろへ回す。
    """
    order = list(Nl2SqlParams.__dataclass_fields__)
    return sorted(
        keys,
        key=lambda key: (order.index(key) if key in order else len(order), str(key)),
    )


#: 評価データの版が記録される前の実験の表示
UNRECORDED_DATASET = "(記録前)"


def dataset_label(experiment: Nl2SqlExperiment) -> str:
    """評価データの版。記録前の実験は `(記録前)`。

    版が違えばスコアは比較できない。ID 3〜8 は記録を始める前のもので、
    **遡って特定する手段が無い**ため空のまま扱う。
    """
    return experiment.dataset_short or UNRECORDED_DATASET


def dataset_hue(digest: str | None) -> int | None:
    """版に当てる色相。同じ版が同じ色になればよい。

    画面に出す8桁から引く。**色の一覧を持たない**のは、版が増えても色を足す手間が
    要らず、一覧・比較のどの画面でも同じ版が必ず同じ色になるため。

    色相を30度刻みの12段に落とすのは、違う版どうしを必ず離すため。
    ダイジェストをそのまま色相にすると、実データの `d7402f21` と `ef3b87a5` が
    209度と133度のように近い値を引くことがあり、並べても見分けがつかない。
    12段なら 150度（緑）と 30度（橙）に分かれる。

    段数が12なので、**違う版が同じ色になることは起こり得る**。色は補助で、
    版の同一性は8桁の文字列で見るもの、という前提を崩さない範囲にとどめる。
    """
    if not digest:
        return None
    try:
        return int(digest[:8], 16) % 12 * 30
    except ValueError:
        return None


def warnings_for(experiment: Nl2SqlExperiment) -> list[str]:
    """そのスコアを額面通りに読んではいけない理由。

    見落とすと評価そのものが無意味になるため、一覧・詳細・比較のどこでも同じ文を出す。
    """
    messages = []
    if experiment.gold_failed:
        messages.append(
            f"正解SQLが実行できなかった問題が {experiment.gold_failed} 件あります"
            "（評価データの不備）"
        )
    if experiment.aborted:
        messages.append(f"途中で打ち切られました: {experiment.abort_reason}")
    return messages


def has_details(experiment_id: int) -> bool:
    """明細ファイルがあるか。

    `read_details()` は中身まで読むので、一覧で行数ぶん呼ぶと要らないI/Oが走る。
    一覧に要るのは有無だけなので存在確認で足りる。
    """
    return details_path(experiment_id).exists()


def varying_parameter_keys(parameter_dicts) -> set[str]:
    """実験ごとに値が違うパラメータのキー。

    一覧のパラメータ列は、全実験で同じ値のキーが大半を占めて長くなる。
    同じ値のキーは**実験の違いを表していない**ので、そこは畳んでよい。

    **どのキーを隠すかは決め打ちにしない。** 何を振るかは実験のたびに変わるため、
    「今ある実験の間で実際に値が割れているか」だけで決める。
    片方に記録が無いキーも「違う」として扱う（既定値で補完すると
    「記録が無い」と「既定値だった」が区別できなくなる）。
    """
    dicts = [parameters or {} for parameters in parameter_dicts]
    if len(dicts) < 2:
        # 1件しか無ければ比べる相手がいない。畳まずに全部見せる
        return set()

    keys = set().union(*dicts)
    return {
        key
        for key in keys
        # 値は基本スカラーだが、比較のためだけに repr へ落として型を問わないようにする
        if len({repr(parameters.get(key, _MISSING)) for parameters in dicts}) > 1
    }


def varying_parameter_line(parameters: dict | None, keys) -> str:
    """差のあるキーだけを並べた1行。差が無ければ空文字。"""
    if not keys:
        return ""
    parameters = parameters or {}
    return ", ".join(
        f"{key}: {parameters.get(key, UNRECORDED)}" for key in sort_parameter_keys(keys)
    )


class ExperimentRow:
    """一覧の1行。モデルをそのまま持ち、表示用の値だけ足す。"""

    def __init__(self, experiment: Nl2SqlExperiment, varying_keys=()):
        self.experiment = experiment
        self.parameter_line = parameter_line(experiment.parameters)
        self.varying_parameter_line = varying_parameter_line(
            experiment.parameters, varying_keys
        )
        self.warnings = warnings_for(experiment)
        self.has_details = has_details(experiment.id)
        self.dataset_label = dataset_label(experiment)
        self.dataset_hue = dataset_hue(experiment.dataset_digest)


def build_list_context() -> dict:
    """一覧画面のコンテキスト。並び順はモデルの `Meta.ordering`（`-id`）に従う。"""
    experiments = list(Nl2SqlExperiment.objects.all())
    varying_keys = varying_parameter_keys(
        [experiment.parameters for experiment in experiments]
    )
    rows = [ExperimentRow(experiment, varying_keys) for experiment in experiments]
    return {
        "rows": rows,
        "has_warning": any(row.warnings for row in rows),
        "has_varying_params": bool(varying_keys),
        "missing_details_count": sum(1 for row in rows if not row.has_details),
    }
