"""freshness_probe: 実表を測って `jss_dataset_freshness` へ記録する（観測層）。

**記録だけを行う。判定も通知もしない**（判定は `jobs/ops_check.py`、通知は
`.github/workflows/ops_check.yml`）。`ops_check` の「何も書かない」原則を保つため、
記録はこの別ジョブに切り出してある。

## なぜ writer の自己申告にしなかったか

`cloud_store/ops.py:record_freshness()` は前の変更で**定義されたが呼び出し箇所が
0 件**のまま残り、本番の `jss_dataset_freshness` は 0 行だった。一方
`ops_check.py` は「鮮度表が空なら失敗」と実装済みで、ワークフローが日次で
`if: failure()` の Issue コメントを足していた。**緑になる経路が存在せず、毎日
Issue にコメントが付き続ける**状態だった。

素直な直し方は「各収集ジョブの最後に自分の鮮度を申告させる」だが、それだと
壊れた writer が「新鮮だ」と自己申告できる（EDINET は 11 営業日連続
processed=0 で「成功」していた）。だから**観測ジョブが実表を測る**。

## 実装上の落とし穴（踏んだ跡を残す）

- **`ctx.cloud` を使わない。** `runner.py` は `if not settings.dry_run:` の中でしか
  `ctx.cloud` を作らないので、`ctx.cloud` に依存すると `--dry-run` が必ず即失敗する
  （既存の `ops_check` がこの不具合を持っていたので同時に直した）。
  `D1Store(ctx.settings.cloud_store, writer=JOB_NAME)` を直接作る。
- **1 件でも測れなければジョブ全体を失敗させる。** `runner._status` は
  failed>0 かつ processed>0 を「一部失敗」＝ **exit 0** にするので、素直に書くと
  表名のタイプミスで 7 件中 1 件が落ちても Issue が立たない。凍結した 1 行が SLO を
  超えるまで（優待なら最長 50 日）気づけないので、最後に例外を投げて
  STATUS_FAILURE へ落とす。
- **`updated_at` に記録時刻を入れない。** 入れると翌日から恒久的に緑になる
  （`ops.record_freshness` の docstring 参照）。
- **観測先は正本 DB だけ。** 全表が 1 DB に同居していることを確認済み
  （2 DB 前提のフォールバックは L-04 で削除）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..cloud_store.d1 import D1Error, D1Store
from ..cloud_store.datasets import DATASET_SOURCES, DatasetSource
from ..cloud_store.ops import safe_record_freshness
from ..config import CloudStoreSettings
from ..models import JST
from .runner import JobContext, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "freshness_probe"

# 取得時刻が測れなかったときに書く値。`jss_dataset_freshness.updated_at` は
# NOT NULL なので NULL を書けない。0 を「不明」として扱うのは既存の
# `slo.age_hours`（`if not updated_at_epoch: return None`）と同じ約束で、
# テスト `test_記録が無ければ緑にしない` もその前提で書かれている。
#
# ここを NULL のまま書こうとすると、0 行の jss_financials で毎回 INSERT が
# 失敗し、observed 7 件中 1 件が常に落ちてジョブが毎日赤になる（= 直したい
# 「毎日鳴る」状態に自分で戻る）。
UNKNOWN_EPOCH = 0


class ProbeIncomplete(RuntimeError):
    """1 データセットでも測れなかった（runner に STATUS_FAILURE を出させるため）。"""


def _coerce_epoch(value: Any) -> int | None:
    """観測した取得時刻を epoch 秒にする。読めなければ None。

    移行元 kabulab-cf が所有する表（`core_stocks` / `yutai_benefits` など）の
    `updated_at` は、この worktree に DDL が無く型を宣言で確認できない。epoch の
    INTEGER と ISO8601 の TEXT のどちらでも観測できるようにしておく
    （採らなかった案: SQL 側で `strftime` を決め打ちする。型を外すと静かに
    NULL になり「鮮度不明」が「表が空」と区別できなくなる）。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    text = str(value).strip()
    try:
        return int(float(text))
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("取得時刻として読めない値: %r", value)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)  # D1 の素の文字列は JST 前提で書かれている
    return int(parsed.timestamp())


def _store(settings: CloudStoreSettings) -> D1Store:
    """観測先（正本 DB）の D1Store を作る。"""
    return D1Store(settings, writer=JOB_NAME)


def _observe(store: D1Store, source: DatasetSource) -> dict[str, Any]:
    """1 データセットを測る。D1Error はそのまま呼び出し側へ渡す。"""
    rows = store.query(source.sql)
    row = rows[0] if rows else {}
    count = row.get("n")
    return {
        "dataset": source.dataset,
        "store_name": source.store,
        "location": source.location,
        "writer": source.writer,
        "latest_data_date": row.get("latest_date"),
        "row_or_object_count": int(count) if count is not None else None,
        # R2 に list API が無く総量を測れない。推測で埋めない（§3-1）。
        "bytes_": None,
        "license_tag": source.license_tag,
        # データ自身の as_of。記録時刻ではない。
        "updated_at": _coerce_epoch(row.get("source_epoch")) or UNKNOWN_EPOCH,
    }


def execute(ctx: JobContext) -> None:
    settings = ctx.settings.cloud_store
    if not settings.d1_enabled():
        ctx.add_failure(
            "d1", "D1 が未設定 (CF_ACCOUNT_ID / CF_API_TOKEN / CF_D1_DATABASE_ID)。実表を測れない"
        )
        return
    store = _store(settings)
    dry_run = ctx.settings.dry_run

    for source in DATASET_SOURCES:
        try:
            observed = _observe(store, source)
        except D1Error as exc:
            # 1 表の欠損で 7 件全滅させない。ここは当該データセットだけ失敗にして次へ。
            ctx.add_failure(source.dataset, f"実表を測れない ({source.location}): {exc}")
            continue

        if dry_run:
            # dry-run は 1 バイトも書かない。代わりに書こうとした内容を全項目出す。
            logger.info(
                "dry-run: %s へ記録しない内容 dataset=%s store=%s location=%s writer=%s"
                " latest_data_date=%s row_or_object_count=%s bytes=%s license_tag=%s"
                " updated_at=%s",
                "jss_dataset_freshness",
                observed["dataset"], observed["store_name"], observed["location"],
                observed["writer"], observed["latest_data_date"],
                observed["row_or_object_count"], observed["bytes_"],
                observed["license_tag"], observed["updated_at"],
            )
            ctx.add_success()
            continue

        # 記録先は必ず正本（jss_dataset_freshness は stockStock 所有）。
        if not safe_record_freshness(store, **observed):
            ctx.add_failure(source.dataset, "鮮度を記録できない（D1 への書き込みが失敗）")
            continue
        logger.info(
            "%s: latest_data_date=%s rows=%s updated_at=%s",
            observed["dataset"], observed["latest_data_date"],
            observed["row_or_object_count"], observed["updated_at"],
        )
        ctx.add_success()

    if ctx.failed:
        # runner は failed>0 かつ processed>0 を exit 0 にするので、ここで例外を投げて
        # 失敗に落とす。落とさないと「7 件中 1 件だけ凍結」が誰にも見えない。
        raise ProbeIncomplete(
            f"{ctx.failed}/{len(DATASET_SOURCES)} データセットを測れなかった:"
            f" {ctx.failed_codes}"
        )


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("実表を測って鮮度を D1 へ記録する（判定も通知もしない）")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
