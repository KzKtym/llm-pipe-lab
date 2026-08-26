"""閲覧のみの3画面。

ビューは「引数を受け取る／404を出す／テンプレートを選ぶ」だけを持つ。
表示用の整形は `web/services/` にある。依存の向きは
`views → services → core / store` の一方向で、**ビューから `core/` は呼ばない**。
"""
from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render

from . import services


def experiment_list(request):
    """一覧 `/nl2sql/`。"""
    return render(request, "nl2sql/list.html", services.build_list_context())


def experiment_detail(request, experiment_id: int):
    """詳細 `/nl2sql/<id>/`。"""
    context = services.build_detail_context(experiment_id)
    if context is None:
        raise Http404(f"実験が見つかりません: {experiment_id}")
    return render(request, "nl2sql/detail.html", context)


def compare(request):
    """比較 `/nl2sql/compare/?a=<id>&b=<id>`。

    指定が揃わない場合は一覧へ戻す。比較は2件でしか成立しないため、
    足りない状態の画面を作っても選び直す以外にできることが無い。
    """
    raw_a = request.GET.get("a", "")
    raw_b = request.GET.get("b", "")

    try:
        id_a, id_b = int(raw_a), int(raw_b)
    except ValueError:
        messages.error(request, "比較する実験を2件指定してください")
        return redirect("nl2sql_list")

    if id_a == id_b:
        messages.error(request, "同じ実験どうしは比較できません")
        return redirect("nl2sql_list")

    context = services.build_compare_context(id_a, id_b)
    if context is None:
        messages.error(request, f"実験が見つかりません（a={id_a}, b={id_b}）")
        return redirect("nl2sql_list")

    return render(request, "nl2sql/compare.html", context)


def stage2_walkthrough(request, experiment_id: int = None):
    """段階2の振る舞い `/nl2sql/stage2/`（最新）/ `/nl2sql/stage2/<id>/`。

    既存3画面と同じく **GET しか受けない**。読むのは保存済みの実験結果だけで、
    この画面から段階2を走らせることはしない（issue_v001 の方針を踏襲）。
    """
    context = services.build_stage2_context(
        experiment_id, show_all=request.GET.get("all") == "1"
    )
    if context is None:
        raise Http404(f"段階2の実験が見つかりません: {experiment_id}")
    return render(request, "nl2sql/stage2.html", context)
