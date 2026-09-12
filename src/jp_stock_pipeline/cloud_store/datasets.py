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
- **存在を確認できていない列を SQL に書かない。** 移行元所有の表の DDL はこの
  リポジトリに無く、列名を間違えると `no such column` で 1 データセットが落ち、
  観測ジョブ全体が毎日失敗する（テスト側の DDL はこのファイルの想定を写すので
  綴り間違いを検出できない）。だから設計書か既存コードで存在が裏付けられる列
  だけを使う。`ir_disclosures` の取得時刻列（`ingested_at` 等）は設計書にも
  既存コードにも出てこないので使わず、`pubdate`（設計書 §A-5 が NOT NULL と
  明記）だけで日付と source_epoch の両方を作る。source_epoch はデータ基準日が
  読めたときには使われないフォールバックなので、未確認の列を足す価値が無い。
  副作用として `pubdate` が epoch ではなく文字列だった場合も、
  `freshness_probe._coerce_epoch` が ISO 文字列を解釈するので unknown に落ちない。

## どの D1 を測るか（間違えると毎日必ず失敗する）

`core_stock_financials` / `ir_disclosures` / `core_stocks` / `yutai_benefits` は
**移行元 kabulab-cf 所有の表**で、正本 DB（`CF_D1_DATABASE_ID`）に同居している
とは限らない。設計の目標形は「D1 は1個」だが、`KABULAB_D1_DATABASE_ID` という
別 secret が現に存在し、`jobs/yutai_backup.py` は `yutai_benefits` をその DB から
読み、`jobs/core_stocks_migrate.py` は `kabulab_d1_database_id or d1_database_id`
で解決している。統合が終わるまでは 2 DB でも動く必要がある。

そこで `db` を宣言で持ち、観測側が DB を選ぶ。`kabulab` でも secret 未設定なら
正本 DB へフォールバックするので、同居済みの環境でも 2 DB の環境でも同じコードで
動く。**ここを正本 DB 固定にすると、表が別 DB にあった場合に 7 件中 4 件が
`no such table` で落ち、観測ジョブが毎日失敗する**（= 潰したかった「毎日鳴る」に
自分で戻る。しかもテスト側の DDL は同一 DB を前提に置くので検出できない）。

## bytes を測らないこと

`bytes_` は全件 None。`r2.py` に list API が無く R2 の総量を測る手段が無い
ため、埋めるには推測しかない（§3-1 推測しない）。R2 の列挙を足すのは別の
変更にする。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..licensing import LicenseTag, inherit

# ③財務サマリは EDINET(commercial-ok) と TDnet(factual-cite) が**同じ表に混ざる**。
# dataset レベルのタグは固定文字列にせず、混ざる可能性のあるタグから
# `licensing.inherit`（= 最も厳しい側）で導く。文字列で書くと、将来 personal-only
# の経路が1つ足された瞬間に緩いタグが残って公開面のフィルタを素通りする。
# なお行単位のタグは `cloud_store/financials.py` が upsert のたびに厳しい側へ
# マージするので、行とデータセットで別々の規則を持つことにはならない。
FINANCIALS_LICENSE_TAG = inherit(
    [LicenseTag.COMMERCIAL_OK, LicenseTag.FACTUAL_CITE]
).value


# 観測先 D1 の識別子。文字列リテラルを散らすと綴り間違いが静かに
# 「正本 DB を見る」へ倒れるので定数で持つ。
DB_CANONICAL = "canonical"
DB_KABULAB = "kabulab"


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
    # 観測先の D1。'canonical' = CF_D1_DATABASE_ID（stockStock の正本）/
    # 'kabulab' = KABULAB_D1_DATABASE_ID（移行元。未設定なら正本へフォールバック）。
    # 既定を canonical にしてあるのは、jss_* は必ず正本にあるため。
    db: str = DB_CANONICAL


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
        db=DB_KABULAB,
    ),
    DatasetSource(
        dataset="tdnet_disclosures",
        store="D1",
        location="ir_disclosures",
        writer="tdnet_hourly",
        sql=(
            "SELECT date(MAX(pubdate),'unixepoch','+9 hours') AS latest_date,"
            " MAX(pubdate) AS source_epoch, COUNT(*) AS n FROM ir_disclosures"
        ),
        license_tag=LicenseTag.FACTUAL_CITE.value,
        note=(
            "pubdate は INTEGER epoch。JST へ寄せてから日付化する。"
            "source_epoch も pubdate を使う（取得時刻の列に頼らない理由は下記）"
        ),
        db=DB_KABULAB,
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
        # **stockStock のジョブ名を書かない。** この表の行を今日書いているのは
        # kabulab-cf の `src/cron/universe.ts` だけで、stockStock 側は
        # `core_stocks_migrate` が ALTER / CREATE INDEX しか出さず、値の充填は
        # `cloud_store/core_stocks.build_column_update` が「組み立てて返す
        # （実行しない）」設計である。ここに `master_sync` と書いていたので、
        # `jss_dataset_freshness.writer` と `jss_writer_claims` の突合
        # （設計書 §1.3-3）が食い違う状態だった。claim は
        # `governance.WRITER_CLAIMS` の `core_stocks/base` が正で、P4b の
        # writer 交代のときに両方を同じ PR で動かす。
        writer="kabulab-cf universe.ts",
        sql=(
            "SELECT NULL AS latest_date, MAX(updated_at) AS source_epoch,"
            " COUNT(*) AS n FROM core_stocks"
        ),
        license_tag=LicenseTag.PERSONAL_ONLY.value,
        note=(
            "データ基準日の列が無い（src_data_date は実測で全行 NULL）ので"
            "`updated_at` しか手が無い。ただしこの列は**行を書いた時刻**で"
            "（`cloud_store/core_stocks.py` が `updated_at = (unixepoch())` を置く）、"
            "データ自身の as_of ではない。writer が古い値を書き直すだけでも進むので"
            "「取得はできたが中身が更新されていない」は検知できない。"
            "`src_fetched_at` が埋まったらそちらへ寄せる（P4a 直後は全行 NULL で、"
            "今これを使うと恒久的に unknown = 毎日鳴る）。"
            "EDINET由来(commercial-ok)と JPX由来(personal-only)が1行に"
            "混在するため、行としては最も厳しい personal-only へ倒す。"
            "commercial-ok にすると JPX 由来の断面メタが公開 API に出る"
        ),
        db=DB_KABULAB,
    ),
    DatasetSource(
        dataset="financials",
        store="D1",
        location="jss_financials",
        # ③ は**単一 writer に出来ない**。EDINET の 1Q/3Q は 2024-06-20 で
        # 途切れて以降 TDnet にしか無く、EPS と 1 株配当は TDnet 側にしか無い
        # （`docs/TARGET-ARCHITECTURE.md` §4.2）。単一名を宣言すると、二重
        # writer 検知を入れたときに片方が「想定外の writer」として毎回落ちる。
        writer="edinet_daily / tdnet_hourly",
        sql=(
            "SELECT MAX(data_date) AS latest_date, MAX(fetched_at) AS source_epoch,"
            " COUNT(*) AS n FROM jss_financials"
        ),
        license_tag=FINANCIALS_LICENSE_TAG,
        note=(
            "EDINET(commercial-ok) と TDnet 短信(factual-cite) が混ざる表なので"
            "タグは厳しい側 = factual-cite。0 件は unknown ではなく red として出す。"
            "source_epoch は `fetched_at`（NOT NULL）。`disclosed_at` は nullable な"
            "開示時刻で、全行 NULL だと行があるのに unknown へ倒れる"
        ),
    ),
    DatasetSource(
        dataset="yutai_benefits",
        store="D1",
        location="yutai_benefits",
        # ここも `core_stocks` と同じ取り違え。`jobs/yutai_backup.py` はこの表を
        # **読んで R2 へ退避する**だけで 1 行も書かない（`cloud_store/yutai.py`
        # の SQL は SELECT のみ）。行を書いているのは kabulab-cf で、
        # claim は `governance.WRITER_CLAIMS` の `yutai_benefits/base`。
        writer="kabulab-cf",
        sql=(
            "SELECT NULL AS latest_date, MAX(updated_at) AS source_epoch,"
            " COUNT(*) AS n FROM yutai_benefits"
        ),
        license_tag=LicenseTag.PERSONAL_ONLY.value,
        note=(
            "データ基準日の列が無い。みんかぶ由来で personal-only。"
            "`updated_at` は core_stocks と同じく記録時刻寄りの列である点に注意"
        ),
        db=DB_KABULAB,
    ),
)

DATASET_SOURCE_BY_NAME: dict[str, DatasetSource] = {d.dataset: d for d in DATASET_SOURCES}

__all__ = [
    "DATASET_SOURCES",
    "DATASET_SOURCE_BY_NAME",
    "DB_CANONICAL",
    "DB_KABULAB",
    "DatasetSource",
]
