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

## どの D1 を測るか

全表が正本 DB（`CF_D1_DATABASE_ID`）の 1 DB に同居していることを確認済み
（本番 PRAGMA を正本 DB で確認。2 DB 前提のフォールバックは L-04 で削除）。
観測は正本 DB だけを見る。

## bytes を測らないこと

`bytes_` は全件 None。`r2.py` に list API が無く R2 の総量を測る手段が無い
ため、埋めるには推測しかない（§3-1 推測しない）。R2 の列挙を足すのは別の
変更にする。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..licensing import LicenseTag, inherit
from .governance import TABLE_LICENSE, TableKind

# ③財務サマリは EDINET(commercial-ok) と TDnet(factual-cite) が**同じ表に混ざる**。
# dataset レベルのタグは固定文字列にせず、混ざる可能性のあるタグから
# `licensing.inherit`（= 最も厳しい側）で導く。文字列で書くと、将来 personal-only
# の経路が1つ足された瞬間に緩いタグが残って公開面のフィルタを素通りする。
# なお行単位のタグは `cloud_store/financials.py` が upsert のたびに厳しい側へ
# マージするので、行とデータセットで別々の規則を持つことにはならない。
FINANCIALS_LICENSE_TAG = inherit(
    [LicenseTag.COMMERCIAL_OK, LicenseTag.FACTUAL_CITE]
).value


def _uniform_tag(table: str) -> str:
    """`TABLE_LICENSE` の uniform 表のタグを引く（L-37）。

    datasets 側にタグのリテラルを置くと地図と二重宣言になり、どちらが
    古いか分からなくなる。表全体が 1 タグの uniform 表は地図が正。
    uniform でない表（行タグ・列地図・絞り込み観測）はここでは導出せず、
    呼び出し側が理由付きのリテラルを持つ。
    """
    spec = TABLE_LICENSE[table]
    if spec.kind is not TableKind.UNIFORM or spec.tag is None:
        raise ValueError(f"{table} は uniform ではないため地図から導出できない")
    return spec.tag.value


@dataclass(frozen=True)
class DatasetSource:
    """1 データセットの観測元。宣言のみ（判定も I/O も持たない）。

    観測先は正本 DB（CF_D1_DATABASE_ID）だけ。2 DB 前提の `db` フィールドは
    L-04 で削除した。
    """

    dataset: str
    store: str  # 'D1' | 'R2'
    location: str  # 表名（絞り込み条件があれば併記して人が読めるようにする）
    writer: str  # 唯一の writer として想定しているジョブ名
    sql: str  # latest_date / source_epoch / n を返す1文
    license_tag: str  # 混在する表は最も厳しいタグへ倒す（licensing._STRICTNESS の順）
    note: str


# dataset のキー集合は `slo.SLO_BY_DATASET` と `slo.NOT_REFRESHED`（更新しないので
# 判定はしないが観測は続けるデータセット）の**和と完全一致**させる
# （tests/test_ops_slo.py が等号で検証する。片方だけ増えたら落ちる）。
DATASET_SOURCES: tuple[DatasetSource, ...] = (
    DatasetSource(
        # stockStock の prices_daily は廃止した。日足断面の実 writer は
        # kabulab-cf の daily.ts で、旧キー 'prices_daily' の行は D1 に残るが
        # 観測対象はこのキーだけ（旧行は無視される）。
        dataset="d1_core_stock_financials",
        store="D1",
        location="core_stock_financials",
        writer="kabulab-cf daily.ts",
        sql=(
            "SELECT MAX(data_date) AS latest_date, MAX(fetched_at) AS source_epoch,"
            " COUNT(*) AS n FROM core_stock_financials"
        ),
        license_tag=_uniform_tag("core_stock_financials"),
        note="yfinance 継承で personal-only。data_date は JST 営業日の文字列",
    ),
    DatasetSource(
        dataset="tdnet_disclosures",
        store="D1",
        location="ir_disclosures",
        writer="tdnet_hourly",
        # `COUNT(*)` を付けると索引シークが全走査に落ちる（実測 37,533 行）。
        # `n` は存在 (0/1) だけ見れば足りるので `MAX` の NULL 判定にする
        # （実測 1 行。L-17）。`yutai_benefits` / `d1_core_stock_financials`
        # には同じ効果が無い（`MAX` 列に索引が無く走査が必須）のを確認済みで、
        # そちらは件数シグナルを残すため `COUNT(*)` のまま。
        sql=(
            "SELECT date(MAX(pubdate),'unixepoch','+9 hours') AS latest_date,"
            " MAX(pubdate) AS source_epoch,"
            " CASE WHEN MAX(pubdate) IS NULL THEN 0 ELSE 1 END AS n"
            " FROM ir_disclosures"
        ),
        license_tag=_uniform_tag("ir_disclosures"),
        note=(
            "pubdate は INTEGER epoch。JST へ寄せてから日付化する。"
            "source_epoch も pubdate を使う（取得時刻の列に頼らない理由は下記）。"
            "n は件数ではなく存在 (0/1)"
        ),
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
        # 行タグ表の絞り込み観測（EDINET 分だけ）。地図は表単位なので導出しない。
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
        # 行タグ表の絞り込み観測（日証金分だけ）。地図は表単位なので導出しない。
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
        # **stockStock のジョブ名を書かない。** この鮮度が測る `MAX(updated_at)`
        # を進めるのは kabulab-cf の `src/cron/universe.ts`（claim の
        # `core_stocks/base`）だけである。stockStock の `master_sync` は
        # `sector33`（`core_stocks/enrich`）を埋めるが、`updated_at` を**進めない**
        # 専用の UPDATE（`core_stocks.build_sector33_updates`）で書くので、
        # この鮮度には現れない。ここに `master_sync` と書くと、測っている列の
        # writer と名前が食い違う（以前そう書いていて §1.3-3 の突合が割れていた）。
        writer="kabulab-cf universe.ts",
        sql=(
            "SELECT NULL AS latest_date, MAX(updated_at) AS source_epoch,"
            " COUNT(*) AS n FROM core_stocks"
        ),
        # 列地図表（EDINET と JPX が 1 行に混在）。地図に表単位のタグが
        # 無いので、厳しい側への倒しをここに書く（理由は下の note）。
        license_tag=LicenseTag.PERSONAL_ONLY.value,
        note=(
            "データ基準日の列が無い（src_data_date は実測で全行 NULL）ので"
            "`updated_at` しか手が無い。ただしこの列は**行を書いた時刻**で"
            "（kabulab-cf の universe.ts が `updated_at = (unixepoch())` を置く。"
            "stockStock の sector33 充填は `updated_at` を進めない）、"
            "データ自身の as_of ではない。writer が古い値を書き直すだけでも進むので"
            "「取得はできたが中身が更新されていない」は検知できない。"
            "`src_fetched_at` が埋まったらそちらへ寄せる（P4a 直後は全行 NULL で、"
            "今これを使うと恒久的に unknown = 毎日鳴る）。"
            "EDINET由来(commercial-ok)と JPX由来(personal-only)が1行に"
            "混在するため、行としては最も厳しい personal-only へ倒す。"
            "commercial-ok にすると JPX 由来の断面メタが公開 API に出る"
        ),
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
        # ⑨優待の退避（旧 `yutai_backup`）は完了し、退避コードは削除した。
        # 行を書いているのは kabulab-cf で、
        # claim は `governance.WRITER_CLAIMS` の `yutai_benefits/base`。
        writer="kabulab-cf",
        sql=(
            "SELECT NULL AS latest_date, MAX(updated_at) AS source_epoch,"
            " COUNT(*) AS n FROM yutai_benefits"
        ),
        license_tag=_uniform_tag("yutai_benefits"),
        note=(
            "データ基準日の列が無い。みんかぶ由来で personal-only。"
            "`updated_at` は core_stocks と同じく記録時刻寄りの列である点に注意"
        ),
    ),
)

DATASET_SOURCE_BY_NAME: dict[str, DatasetSource] = {d.dataset: d for d in DATASET_SOURCES}

__all__ = [
    "DATASET_SOURCES",
    "DATASET_SOURCE_BY_NAME",
    "DatasetSource",
]
