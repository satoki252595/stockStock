"""ジョブ結果と鮮度を D1 へ記録する（検知層）。

`jss_job_runs` と `jss_dataset_freshness` は器だけ作られて **どちらも 0 行**
のまま放置されていた。writer が両リポジトリに1つも無かったため、
`/v1/meta/freshness` は 200 で空配列を返し、外から見ると「鮮度情報が無い」
ではなく「問題なし」に読める状態だった。

実際に次を誰も検知できなかった:
- EDINET の日次収集が **11 営業日連続** processed=0 のまま「成功」
- JPX の 404 で銘柄マスタが **33 日**停止
- 優待が **81.5 日**停止

ここは「記録する」だけを担う。閾値の判定と通知は
`jobs/ops_check.py` が行う（記録と判定を混ぜない）。
"""

from __future__ import annotations

import json
import logging
from datetime import date

from .d1 import D1Error, D1Store

logger = logging.getLogger(__name__)

# 1 行の rich_text 上限に合わせる。全量はジョブ標準出力に残る。
MAX_FAILED_CODES = 50


def record_job_run(
    store: D1Store,
    *,
    job_name: str,
    status: str,
    processed: int,
    failed: int,
    failed_codes: list[str],
    run_url: str | None,
    duration_secs: float | None,
    finished_at: int,
) -> None:
    """1 回の実行結果を残す。失敗しても呼び出し側のジョブ結果は変えない。

    書き込みのたび 90 日より古い行を剪定する (L-17)。
    空振り検知の 30 日窓より長く保ち、履歴が無限に育たないようにする。
    """
    store.query(
        "DELETE FROM jss_job_runs WHERE finished_at < strftime('%s','now','-90 days')"
    )
    store.query(
        "INSERT INTO jss_job_runs"
        " (job_name, status, processed, failed, failed_codes, run_url,"
        "  duration_secs, finished_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            job_name,
            status,
            processed,
            failed,
            json.dumps(failed_codes[:MAX_FAILED_CODES], ensure_ascii=False)
            if failed_codes
            else None,
            run_url,
            duration_secs,
            finished_at,
        ],
    )


def record_freshness(
    store: D1Store,
    *,
    dataset: str,
    store_name: str,
    location: str,
    writer: str,
    latest_data_date: date | str | None,
    row_or_object_count: int | None,
    bytes_: int | None,
    license_tag: str | None,
    updated_at: int,
) -> None:
    """データセットの鮮度断面を上書きする（1 データセット 1 行）。

    **`updated_at` は「記録した時刻」ではなく「観測した実表の取得 epoch」**
    （= データ自身の as_of）を渡すこと。ここに `now` を入れると、実表が凍結して
    いても記録のたびに更新時刻が進み、**翌日から恒久的に緑**になる。それは
    「鮮度表があるのに何も検知しない」という、writer 不在より悪い状態になる
    （空なら少なくとも「空だ」と分かる）。

    `latest_data_date` も同様にデータ基準日であり、判定はこちらを優先する
    （`slo.judge_observation` の (c)）。`updated_at` は基準日の列が無い表の
    フォールバックにしか使わない。

    そのフォールバックの限界も書いておく: 基準日の列が無い表（`core_stocks` /
    `yutai_benefits`）で渡せるのは、その表自身の `updated_at`＝**その表の writer が
    行を書いた時刻**である。観測ジョブの `now` ではないので「記録のたびに進む」
    ことは無いが、writer が同じ値を書き直すだけでも進むため「取得はできたが中身が
    更新されていない」は検知できない。日付列が埋まったらそちらへ寄せる。
    """
    latest = (
        latest_data_date.isoformat()
        if isinstance(latest_data_date, date)
        else latest_data_date
    )
    store.query(
        "INSERT INTO jss_dataset_freshness"
        " (dataset, store, location, writer, latest_data_date,"
        "  row_or_object_count, bytes, license_tag, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(dataset) DO UPDATE SET"
        "  store = excluded.store, location = excluded.location,"
        "  writer = excluded.writer, latest_data_date = excluded.latest_data_date,"
        "  row_or_object_count = excluded.row_or_object_count,"
        "  bytes = excluded.bytes, license_tag = excluded.license_tag,"
        "  updated_at = excluded.updated_at",
        [
            dataset,
            store_name,
            location,
            writer,
            latest,
            row_or_object_count,
            bytes_,
            license_tag,
            updated_at,
        ],
    )


def safe_record_job_run(store: D1Store | None, **kwargs) -> bool:
    """記録に失敗してもジョブを落とさない。記録できたかを返す。

    ここで例外を投げるとジョブ本体の成否を記録層が左右してしまう。
    ただし**黙って握りつぶさない**（警告を出し、返り値で分かるようにする）。
    """
    if store is None:
        return False
    try:
        record_job_run(store, **kwargs)
    except D1Error as exc:
        logger.warning("jss_job_runs へ記録できず: %s", exc)
        return False
    return True


def safe_record_freshness(store: D1Store | None, **kwargs) -> bool:
    """鮮度の記録に失敗してもジョブを落とさない。記録できたかを返す。

    `safe_record_job_run` と同形。ここで例外を投げると「1 表の欠損で 7 件全滅」に
    なるため、呼び出し側が返り値で当該データセットだけを失敗扱いにできるようにする
    （`jobs/freshness_probe.py` はそれを受けてジョブ全体を失敗させる）。

    なお `record_freshness` に `license_tag` の None 拒否は**入れない**。検証は
    マニフェスト（`datasets.py`）側とそのテストで行う。記録層で弾くと、
    ライセンスタグを持たない呼び出しが「記録できない」に化けて原因が分からなくなる。
    """
    if store is None:
        return False
    try:
        record_freshness(store, **kwargs)
    except D1Error as exc:
        logger.warning("jss_dataset_freshness へ記録できず: %s", exc)
        return False
    return True
