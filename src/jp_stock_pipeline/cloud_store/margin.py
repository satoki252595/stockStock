"""JPX 信用残の R2 互換シム書き込み (移行 P2)。

`vwap-data/margin/{date}.json` と `margin/weeks.json` は kabulab-cf の
`/vwap-analysis` が読んでいる**既存の公開面**。writer を stockStock へ移管しても
**読み手から見て1バイトも変わらない**ことが要件なので、契約キーを固定し、
追加キーを入れない。

## 既存週の不可侵

既存10週（2026-06-12〜）のうち 2026-06-12〜07-31 は JPX が公開を終えており
**再取得できない**（07-03・07-10 は恒久欠測）。したがって:
- `weeks.json` は**要素が減る書込を拒否**する（空配列で潰すと、R2 上に
  オブジェクトが残っていても `/api/margin` は weeks.json を唯一の入口に
  しているため画面上はデータが消えたのと同じになる）。
- 既存の `margin/{date}.json` は上書きしない（同じ日付を再取得した場合のみ
  内容が同一であることを前提に PUT する）。
- 削除メソッドは実装しない（cloud_store.r2 に delete が無いのと同じ方針）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .guards import GuardError, check_no_regression

if TYPE_CHECKING:
    from ..collectors.jpx_margin import MarginData
    from .r2 import R2Store

logger = logging.getLogger(__name__)

WEEKS_KEY = "margin/weeks.json"

# margin/{date}.json の契約キー。kabulab-cf の /api/margin が
# 行を丸ごとスプレッドして返すため、**キーを増やすと公開面が広がる**。
SNAPSHOT_CONTRACT: dict[str, tuple[str, ...]] = {
    "$": ("week",),
    "rows[]": ("code", "sell", "sell_chg", "buy", "buy_chg"),
}


def snapshot_key(week: str) -> str:
    """`margin/{YYYY-MM-DD}.json`。既存の命名をそのまま使う。"""
    if not week:
        raise ValueError("week が空。基準日を特定できない PDF は書き込まない")
    return f"margin/{week}.json"


def merge_weeks(existing: list[str] | None, week: str) -> list[str]:
    """weeks.json へ 1 件追加する。**既存要素は決して落とさない。**

    kabulab-cf の実装と同じく「含まれていなければ push してソート」。
    既存が None（初回）でも空配列から始める。
    """
    weeks = list(existing or [])
    if week not in weeks:
        weeks.append(week)
    return sorted(weeks)


def write_margin_snapshot(
    store: "R2Store", data: "MarginData", *, overwrite_existing: bool = False
) -> tuple[str, bool]:
    """`margin/{week}.json` を書く。(キー, 実際に書いたか) を返す。

    既存が有る場合は既定で**書かない**（既存週の不可侵）。同一内容の再取得なら
    書いても無害だが、比較せずに上書きすると差分が出たときに気付けないため、
    既定では触らず呼び出し側に判断させる。
    """
    key = snapshot_key(data.week)
    payload = data.to_dict()
    existing, found = store.get_json(key)
    if found and not overwrite_existing:
        if existing == payload:
            logger.info("margin: %s は既存と同一のためスキップ", key)
        else:
            logger.warning(
                "margin: %s が既存と異なるが上書きしない（既存週の不可侵）。"
                "差分を確認すること: 既存rows=%s 新rows=%s",
                key,
                len((existing or {}).get("rows") or []),
                len(payload["rows"]),
            )
        return key, False
    # 契約キーの検証のみ行う（新規作成なので後退比較の対象は無い）
    check_no_regression(
        existing if found else None,
        payload,
        writer=store.writer,
        contract=SNAPSHOT_CONTRACT,
        require_writer=False,  # 既存オブジェクトに writer キーは無い（契約を変えない）
    )
    store.put_json_guarded(
        key, payload, contract=SNAPSHOT_CONTRACT, add_writer=False
    )
    return key, True


def update_weeks_index(store: "R2Store", week: str) -> list[str]:
    """`margin/weeks.json` へ week を追加する。要素が減る書込は拒否される。

    このファイルは `/api/margin` の唯一の入口なので、空配列で潰すと
    画面上データが消えたのと同じになる。既存10週のうち 2026-06-12〜07-31 は
    JPX から再取得できない。
    """
    existing, _found = store.get_json(WEEKS_KEY)
    if existing is not None and not isinstance(existing, list):
        raise GuardError(
            f"{WEEKS_KEY} が配列ではない（想定外の形式）。上書きしない: {type(existing)}"
        )
    weeks = merge_weeks(existing, week)
    check_no_regression(existing, weeks, writer=store.writer, require_writer=False)
    store.put_json_guarded(WEEKS_KEY, weeks, add_writer=False)
    return weeks


__all__ = [
    "SNAPSHOT_CONTRACT",
    "WEEKS_KEY",
    "merge_weeks",
    "snapshot_key",
    "update_weeks_index",
    "write_margin_snapshot",
]
