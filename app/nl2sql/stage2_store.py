"""段階2実験の明細をファイルへ残す。

段階1の `store.py` と同じ「集計はDB、明細はファイル」の分担だが、置き場は
別の設定キー（`NL2SQL_STAGE2_LOGS_DIR`）にする。段階1のログ置き場と同じキーを
使うと、片方だけテストで上書きしても他方に事故が及ぶため（段階1で実際に
テストDBのidで実データを上書きした事故がある）。

`data/` は Git 管理外。
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from django.conf import settings


def get_logs_dir() -> Path:
    """明細の保存先。`settings.NL2SQL_STAGE2_LOGS_DIR` で上書きできる。

    **テストは必ず上書きすること。** テストDBはロールバックされるが
    ファイルシステムは戻らないため。
    """
    override = getattr(settings, "NL2SQL_STAGE2_LOGS_DIR", None)
    if override:
        return Path(override)
    return Path(settings.BASE_DIR) / "data" / "nl2sql_stage2" / "logs"


def details_path(experiment_id: int) -> Path:
    return get_logs_dir() / f"stage2_exp_{experiment_id}.json"


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
    """明細を読む。無ければ None。"""
    path = details_path(experiment_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def recompute_axis2(experiment_id: int) -> dict:
    """明細から第2軸（人の判定）の集計を数え直す。

    人の判定（`human_correct`）は明細ファイルを直接編集する運用のため、
    集計は必ずこの関数で明細から再計算すること。**手計算では埋めない**
    （手で編集する以上、明細と集計は必ずずれるため。`issue_c009` Q&A2）。

    `axis2_disagreement` の母数は `axis2_human_reviewed`（人が判定を入れた件数）。
    `human_correct` が `None`（未判定）の問題は、一致も不一致も判定できないため
    食い違いに数えない。

    Returns:
        `{"axis2_human_reviewed": int, "axis2_human_correct": int, "axis2_disagreement": int}`

    Raises:
        FileNotFoundError: 明細ファイルが無い場合
    """
    payload = read_details(experiment_id)
    if payload is None:
        raise FileNotFoundError(f"明細ファイルがありません: experiment_id={experiment_id}")

    reviewed = 0
    correct = 0
    disagreement = 0
    for item in payload["items"]:
        if item.get("confusion") != "TP":
            # 第2軸の対象は tp（正しく聞き返せた問題）のみ
            continue
        human = item.get("human_correct")
        if human is None:
            continue
        reviewed += 1
        if human:
            correct += 1
        if bool(human) != bool(item.get("keyword_match")):
            disagreement += 1

    return {
        "axis2_human_reviewed": reviewed,
        "axis2_human_correct": correct,
        "axis2_disagreement": disagreement,
    }
