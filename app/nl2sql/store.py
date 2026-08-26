"""実験の明細をファイルへ残す。

1問ごとの生成SQL・エラー・一致判定は行数が多く、DBの列に入れると
一覧の取り回しが悪くなる。`data/nl2sql/logs/exp_*.json` に外出しする扱いにする。

`data/` は Git 管理外。実験結果は環境ごとのものなので追跡しない。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from django.conf import settings


def get_logs_dir() -> Path:
    """明細の保存先。

    `settings.NL2SQL_LOGS_DIR` で上書きできる。**テストは必ず上書きすること。**

    テストDBはロールバックされるがファイルシステムは戻らない。上書きしないと、
    テストDBで採番された id（1, 2, 3, ...）で実ファイル `exp_1.json` … を
    書き潰してしまう。実際にそれで exp_4〜exp_8 の明細を失った（issue_081415）。
    """
    override = getattr(settings, "NL2SQL_LOGS_DIR", None)
    if override:
        return Path(override)
    return Path(settings.BASE_DIR) / "data" / "nl2sql" / "logs"


def details_path(experiment_id: int) -> Path:
    return get_logs_dir() / f"exp_{experiment_id}.json"


def save_details(experiment_id: int, items, meta: dict | None = None) -> Path:
    """明細を保存し、パスを返す。"""
    path = details_path(experiment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta or {},
        "items": [asdict(item) for item in items],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def read_details(experiment_id: int) -> dict | None:
    """明細を読む。無ければ None。

    実験は消えていないのにファイルだけ無い（別環境で走らせた等）ことが
    あり得るため、例外ではなく None を返す。
    """
    path = details_path(experiment_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
