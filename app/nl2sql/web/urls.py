"""NL→SQL 実験の閲覧画面。

**GET しか受けない。** 実験の実行は画面の責務ではないという方針のため、
ここからは走らせない。star・改名・削除も同じ理由で作らない（issue_v001）。

URL名は移植元（`experiment_list` / `result` / `compare`）と衝突していたため
`nl2sql_` を付けて分けてある。移植元を含まない本リポジトリでは衝突しないが、
名前は変えずに残す。
"""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.experiment_list, name="nl2sql_list"),
    path("compare/", views.compare, name="nl2sql_compare"),
    # 段階2の振る舞いを1枚で見せるページ（issue_v005）。既定は最新の実験
    path("stage2/", views.stage2_walkthrough, name="nl2sql_stage2"),
    path("stage2/<int:experiment_id>/", views.stage2_walkthrough,
         name="nl2sql_stage2_experiment"),
    path("<int:experiment_id>/", views.experiment_detail, name="nl2sql_detail"),
]
