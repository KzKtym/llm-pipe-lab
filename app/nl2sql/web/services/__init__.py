"""ビューに渡す形を組み立てる層。

依存の向きは `views → services → core / store` の一方向。
**ビューから `core/` を直接呼ばない。** 移植元の RAG 実験ツールの `web/services/` と同じ約束。

この層は読み取りしかしない。画面から実験を走らせる導線は作らない方針のため、
書き込み系のサービスは1つも置かない（issue_v001）。
"""
from .compare_service import build_compare_context
from .detail_service import build_detail_context
from .list_service import build_list_context, parameter_line
from .stage2_service import build_stage2_context

__all__ = [
    "build_compare_context",
    "build_detail_context",
    "build_list_context",
    "build_stage2_context",
    "parameter_line",
]
