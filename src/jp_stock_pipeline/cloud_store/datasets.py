"""データセット・マニフェスト: 「どの表を測れば鮮度が分かるか」の宣言だけ。

`slo.py` が閾値を、ここが**測り方**を持つ。I/O も判定もここには置かない
（`jobs/freshness_probe.py` が読み、`jobs/ops_check.py` が判定する）。

## なぜ writer の自己申告にしなかったか

素直な実装は「各収集ジョブが自分の書いた件数と日付を `jss_dataset_freshness`
へ申告する」だが、それだと**壊れた writer が「新鮮だ」と申告できてしまう**。
実測で EDINET は 11 営業日連続 processed=0 で「成功」していた。そのとき
writer の自己申告は「今さっき更新した」になる（実際には1行も入っていない）。
だから観測ジョブが**実表を測る**。writer が黙って死んでも、表は嘘をつかない。

## SQL の制約（構造的に守る）

- 1 データセット = **1 文の集約**。`MAX(...) AS latest_date` /
  `MAX(...) AS source_epoch` / `COUNT(*) AS n` の3列だけを返す。
- **UNION を使わない**。D1 の compound SELECT 上限は
  `d1.MAX_COMPOUND_SELECT_TERMS = 5`（本番実測。素の SQLite の 500 ではない）で、
  「7 データセットを1文で数える」は構造的に上限へ抵触する。1文ずつ7回投げる。
- epoch 列から日付を作るときは必ず `date(x,'unixepoch','+9 hours')`。UTC の
  ままだと他データセットの JST 営業日文字列と 1 日ずれ、同じ表の中で基準が
  2 つになる。

## bytes を測らないこと

`bytes_` は全件 None。`r2.py` に list API が無く R2 の総量を測る手段が無い
ため、埋めるには推測しかない（§3-1 推測しない）。R2 の列挙を足すのは別の
変更にする。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..licensing import LicenseTag


@dataclass(frozen=True)
class DatasetSource:
    """1 データセットの観測元。宣言のみ（判定も I/O も持たない）。"""

    dataset: str
    store: str  # 'D1' | 'R2'
    location: str  # 表名（絞り込み条件があれば併記して人が読めるようにする）
    writer: str  # 唯一の writer として想定しているジョブ名
    sql: str  # latest_date / source_epoch / n を返す1文
    license_tag: str  # 混在する表は最も厳しいタグへ倒す（licensing._STRICTNESS の順）
    note: str


# dataset のキー集合は `slo.SLO_BY_DATASET` と**完全一致**させる
# （tests/test_ops_slo.py が等号で検証する。片方だけ増えたら落ちる）。
DATASET_SOURCES: tuple[DatasetSource, ...] = (
    DatasetSource(
        dataset="prices_daily",
        store="D1",
        location="core_stock_financials",
        writer="prices_daily",
        sql=(
            "SELECT MAX(data_date) AS latest_date, MAX(fetched_at) AS source_epoch,"
            " COUNT(*) AS n FROM core_stock_financials"
        ),
        license_tag=LicenseTag.PERSONAL_ONLY.value,
        note="yfinance 継承で personal-only。data_date は JST 営業日の文字列",
    ),
    DatasetSource(
        dataset="tdnet_disclosures",
        store="D1",
        location="ir_disclosures",
        writer="tdnet_hourly",
        sql=(
            "SELECT date(MAX(pubdate),'unixepoch','+9 hours') AS latest_date,"
            " MAX(ingested_at) AS source_epoch, COUNT(*) AS n FROM ir_disclosures"
        ),
        license_tag=LicenseTag.FACTUAL_CITE.value,
        note="pubdate は INTEGER epoch。JST へ寄せてから日付化する",
    ),
    DatasetSource(
        dataset="edinet_documents",
        store="D1",
        location="jss_raw_files WHERE source='EDINET'",
        writer="edinet_daily",
        sql=(
            "SELECT MAX(data_date) AS latest_date, MAX(last_fetched_at) AS source_epoch,"
            " COUNT(*) AS n FROM jss_raw_files WHERE source = 'EDINET'"
        ),
        license_tag=LicenseTag.COMMERCIAL_OK.value,
        note="11 営業日の空振りを検知できなかった対象。原本索引の EDINET 分だけを測る",
    ),
    DatasetSource(
        dataset="jsf_supply",
        store="D1",
        location="jss_supply_latest WHERE data_type='jsf_zandaka'",
        writer="supply_daily",
        sql=(
            "SELECT MAX(data_date) AS latest_date, MAX(fetched_at) AS source_epoch,"
            " COUNT(*) AS n FROM jss_supply_latest WHERE data_type = 'jsf_zandaka'"
        ),
        license_tag=LicenseTag.PERSONAL_ONLY.value,
        note=(
            "日証金の規約は私的利用限定。fetched_at は毎回 now_jst() で塗り直される"
            "ので、鮮度は必ず data_date で見る"
        ),
    ),
    DatasetSource(
        dataset="core_stocks",
        store="D1",
        location="core_stocks",
        writer="master_sync",
        sql=(
            "SELECT NULL AS latest_date, MAX(updated_at) AS source_epoch,"
            " COUNT(*) AS n FROM core_stocks"
        ),
        license_tag=LicenseTag.PERSONAL_ONLY.value,
        note=(
            "データ基準日の列が無い（src_data_date は実測で全行 NULL）ので取得時刻で"
            "測るしかない。EDINET由来(commercial-ok)と JPX由来(personal-only)が1行に"
            "混在するため、行としては最も厳しい personal-only へ倒す。"
            "commercial-ok にすると JPX 由来の断面メタが公開 API に出る"
        ),
    ),
    DatasetSource(
        dataset="financials",
        store="D1",
        location="jss_financials",
        writer="edinet_daily",
        sql=(
            "SELECT MAX(data_date) AS latest_date, MAX(disclosed_at) AS source_epoch,"
            " COUNT(*) AS n FROM jss_financials"
        ),
        license_tag=LicenseTag.FACTUAL_CITE.value,
        note="現在 0 行（writer 未実装）。0 件は unknown ではなく red として出す",
    ),
    DatasetSource(
        dataset="yutai_benefits",
        store="D1",
        location="yutai_benefits",
        writer="yutai_backup",
        sql=(
            "SELECT NULL AS latest_date, MAX(updated_at) AS source_epoch,"
            " COUNT(*) AS n FROM yutai_benefits"
        ),
        license_tag=LicenseTag.PERSONAL_ONLY.value,
        note="データ基準日の列が無い。みんかぶ由来で personal-only",
    ),
)

DATASET_SOURCE_BY_NAME: dict[str, DatasetSource] = {d.dataset: d for d in DATASET_SOURCES}

__all__ = ["DATASET_SOURCES", "DATASET_SOURCE_BY_NAME", "DatasetSource"]
