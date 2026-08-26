"""段階2（ルール判定版）実験の明細をファイルへ残す。

`stage2_store.py`（旧・LLM総合判断版）と同じ「集計はDB、明細はファイル」の分担だが、
置き場は別の設定キー（`NL2SQL_STAGE2_RULE_LOGS_DIR`）にする。他の段階2ログ置き場と
同じキーを使うと、片方だけテストで上書きしても他方に事故が及ぶため。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from django.conf import settings


def get_logs_dir() -> Path:
    """明細の保存先。`settings.NL2SQL_STAGE2_RULE_LOGS_DIR` で上書きできる。

    **テストは必ず上書きすること。**
    """
    override = getattr(settings, "NL2SQL_STAGE2_RULE_LOGS_DIR", None)
    if override:
        return Path(override)
    return Path(settings.BASE_DIR) / "data" / "nl2sql_stage2_rule" / "logs"


def details_path(experiment_id: int) -> Path:
    return get_logs_dir() / f"stage2_rule_exp_{experiment_id}.json"


def save_details(experiment_id: int, items, meta: dict | None = None) -> Path:
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
    path = details_path(experiment_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
