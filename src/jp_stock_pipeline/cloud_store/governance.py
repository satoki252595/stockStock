"""D1 の表ごとのライセンス区分（地図の第2枚目ではなく、第1枚目の続き）。

`schema.MIXED_LICENSE_COLUMNS` は **1 行にライセンスが混在する表**だけを列単位で
持つ。ここはその外側、つまり「この表全体はどの区分か」を持つ。2 つで 1 枚の地図
であり、どちらにも載っていない (表, 列) が無いことを `jobs/license_map.py` が
毎日確かめる。

## 何が穴だったか

本番 30 表 375 列のうち、行タグも列地図も無いのが **22 表 / 257 列（68.5%）**
だった（2026-09-13 に `sqlite_master` を読み直した実測値。当初の「374 列 /
256 列」は 1 列ぶん少なく数えていた）。`license_tag` 列を物理的に持つ表は 8 つあるが、**行が入って実際に
機能しているのは 3 表だけ**（`jss_index_symbols` 6 行 / `jss_raw_files` 150 行 /
`jss_supply_latest` 4,351 行。`core_stocks.license_tag` は 3,818 行すべて NULL）。

つまり「行タグで判定する」という前提そのものが大半の表で成立していなかった。

## 地図に無い (表, 列) の扱い — fail closed

**宣言が無い列は「公開してよいと決まっていない」= 公開しない。** 地図は
「公開できるもの」の allowlist であって、「公開できないもの」の denylist では
ない。したがって:

- 列地図に無い列は `personal-only` と同じ扱い（公開投影ビューに入れない）
- 未分類の表・列は**報告する**。害は無いが「決めていない」ことが見えないと
  永久に決まらない

## タグは「値が第三者の著作物・データセットを含むか」を答える

「公開 API がその列を出すべきか」ではない。サロゲートキー `core_stocks.id` は
第三者由来の値を 1 バイトも含まないので `commercial-ok` だが、公開 API が出す
べきかは別の判断（出さない。移行元の PK なので）。2 つを混ぜると、API 設計の
都合でライセンスタグが動いてしまう。

## 派生値は最も厳しいタグを継承する（`licensing.inherit`）

`swing_*` / `otakara_*` / `rsi_percentile` / `finmath_*` は Yahoo 日足からの
派生なので `personal-only`。`is_active` のような「入力の存在から決まる真偽値」も
同じ規則に従うが、**それが本当に派生か**は列ごとに判断が要るので、決まって
いないものは宣言しない（推測で埋めない §3-1）。

## スナップショットの所有と更新手順（fail open / closed の境界）

`swing_*` / `otakara_*` / `finmath_*` / `yutai_*` / `ir_*` / `rsi_*` / `yuho_*` は
**kabulab-cf の drizzle 管理下**にあり、あちらが表を足すとこの宣言は古くなる。

- **所有**: この宣言は stockStock が持つ。**「kabulab-cf にテストを回す CI が
  無いから」ではない**（当初そう書いていたが事実誤認。あちらには
  `.github/workflows/ci.yml` があり push / pull_request で `pnpm test` を回す。
  commit 53d327a で追加済み）。持つ理由は、この宣言が **D1 の全表を横断する
  1 枚の地図**であり、本番 `sqlite_master` と突き合わせる実行主体
  (`jobs/license_map.py`) がこちらにしか無いことである。kabulab-cf 側は
  共有契約 `tests/fixtures/contracts/d1-license-map.json` を同一バイト列で
  持ち、自分のテストで読む（CI があるので実際に読ませられる）。
- **更新手順**: `jobs/license_map.py` が本番 `sqlite_master` を読んで「宣言に
  無い表」を名前つきで報告する。報告された表を `TABLE_LICENSE` へ足す PR を
  出す。タグが分からなければ `UNCLASSIFIED` で登録してよい（「未分類だと
  分かっている」は「宣言が無い」より強い）。
- **fail open / closed**: 「宣言に無い表が本番にある」は **warning**（fail
  open）。「宣言にある表が本番から消えた」「行タグ前提の表に `license_tag` 列が
  無い」「列地図にある列が本番から消えた」は **failure**（fail closed）。

  未知の表を即 failure にしない理由は、対向リポジトリの migration のたびに
  毎日赤くなる検査は読まれなくなり、通知を殺すのと同じになるからである
  （設計書 §7.6 を自分自身に適用する）。

  当初ここには「本番 PRAGMA を読めないので 30 表のうち 27 表しか裏付けが無い」
  とも書いていたが、読み取り専用の `sqlite_master` 照会で残り 3 表
  （`swing_market_context` / `swing_sector_daily` / `yutai_genres`）の名前と
  DDL が取れたため 30 表すべてを登録した（2026-09-13 実測）。したがって
  「宣言に無い表」は**今後 kabulab-cf が表を足したときだけ**出る。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from ..licensing import LicenseTag


class TableKind(StrEnum):
    """公開可否をどこで判定するか。"""

    # 行の `license_tag` 列が正。ソースが行ごとに違う表。
    ROW_TAG = "row-tag"
    # 1 行にライセンスが混在する。`schema.MIXED_LICENSE_COLUMNS` が正。
    COLUMN_MAP = "column-map"
    # 表全体が 1 つのタグ。writer が 1 ソースしか使わない表。
    UNIFORM = "uniform"
    # 第三者由来の値を含まない運用・観測用の表。
    OPERATIONAL = "operational"
    # 本番に存在することは分かっているが、タグを決めていない。
    # 「未分類だと分かっている」は「宣言が無い」より強い（fail closed 側に倒す）。
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True)
class TableLicense:
    """1 表ぶんの区分。I/O も判定も持たない宣言。"""

    kind: TableKind
    # UNIFORM のときの表全体のタグ。他の kind では None。
    tag: LicenseTag | None
    # 一次ソース（人が読んで検証できるようにする）。
    source: str
    # この宣言の裏付け（どこで実測・確認したか）。推測で埋めないための欄。
    evidence: str


def _uniform(tag: LicenseTag, source: str, evidence: str) -> TableLicense:
    return TableLicense(kind=TableKind.UNIFORM, tag=tag, source=source, evidence=evidence)


def _operational(evidence: str) -> TableLicense:
    return TableLicense(
        kind=TableKind.OPERATIONAL,
        tag=LicenseTag.COMMERCIAL_OK,
        source="stockStock（運用メタ）",
        evidence=evidence,
    )


def _row_tag(source: str, evidence: str) -> TableLicense:
    return TableLicense(kind=TableKind.ROW_TAG, tag=None, source=source, evidence=evidence)


# 派生テクニカル・バリュエーションの出典はすべて Yahoo 日足。
_YAHOO = "Yahoo Finance 日足からの派生（licensing.inherit で personal-only を継承）"
# 子表 14 本の実測根拠。
_CHILD = "cloud_store/core_stocks.CHILD_TABLES（2026-09-12 本番実測）"

TABLE_LICENSE: dict[str, TableLicense] = {
    # --- ①銘柄マスタ: 1 行に混在する唯一の表 -------------------------------
    "core_stocks": TableLicense(
        kind=TableKind.COLUMN_MAP,
        tag=None,
        source="EDINET コードリスト + JPX data_j.xlsx",
        evidence="schema.MIXED_LICENSE_COLUMNS が正。E7 の列定義ドリフト検出つき",
    ),
    # --- kabulab-cf 所有の派生断面・時系列（Yahoo 由来）---------------------
    "core_stock_financials": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _CHILD),
    "core_stock_annual_financials": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _CHILD),
    "swing_daily_ohlcv": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _CHILD),
    "swing_stock_indicators": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _CHILD),
    "swing_entry_signals": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _CHILD),
    # `swing_stock_screening` は L-52 で indicators の列に畳んで DROP（kabulab-cf 0015）。
    "rsi_percentile": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _CHILD),
    # L2 投影（kabulab-cf PR #23 / 0011 で 2026-09-13 に本番作成）。swing_daily_ohlcv の
    # 終値列を銘柄ごとに 1 行へ畳んだもので、値の出所は Yahoo 日足のまま。
    "p_momentum": _uniform(
        LicenseTag.PERSONAL_ONLY,
        _YAHOO,
        "kabulab-cf src/shared/db/projection-schema.ts（2026-09-13 本番 sqlite_master で作成を確認）",
    ),
    # L2 投影（kabulab-cf K4b / 0017 で作成予定）。yuho_order_facts /
    # yuho_overseas_facts の CAGR・YoY・比率・地域比率を銘柄ごとに 1 行へ畳んだ
    # もので、値の出所は EDINET 有報 XBRL のまま commercial-ok。
    "p_yuho_growth": _uniform(
        LicenseTag.COMMERCIAL_OK,
        "EDINET 有価証券報告書 XBRL からの派生（licensing.inherit で commercial-ok を継承）",
        "kabulab-cf src/shared/db/projection-schema.ts（P4 適用後に本番 sqlite_master で作成を確認する）",
    ),
    "otakara_stock_financials": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _CHILD),
    "otakara_stock_scores": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _CHILD),
    # --- kabulab-cf 所有の開示・優待 ---------------------------------------
    # 開示メタ（いつ・誰が・何を）は事実として引用でき、原文は出さない。
    # `cloud_store/datasets.py` の tdnet_disclosures も factual-cite で宣言済み。
    "ir_disclosures": _uniform(
        LicenseTag.FACTUAL_CITE,
        "TDnet（東証 適時開示）",
        f"{_CHILD} / datasets.py の tdnet_disclosures も factual-cite",
    ),
    # みんかぶ掲載文を含む。設計書 §8.3 は 3 タグでは足りないとして `no-store` の
    # 新設を挙げているが、現行の `licensing.LicenseTag` は 3 値なので最も厳しい
    # personal-only へ倒す。`worker/src/shared/license.ts` は description /
    # short_summary / estimated_value を列単位で伏せている（そちらが実効的な防御）。
    "yutai_benefits": _uniform(
        LicenseTag.PERSONAL_ONLY,
        "みんかぶ掲載文（取得停止済み。TDnet 由来へ移行中）",
        f"{_CHILD} / datasets.py の yutai_benefits も personal-only",
    ),
    # --- kabulab-cf 所有の EDINET 有報 -------------------------------------
    "yuho_documents": _uniform(
        LicenseTag.COMMERCIAL_OK, "EDINET（金融庁）", f"{_CHILD} / EDINET 120・130 のみ"
    ),
    "yuho_order_facts": _uniform(LicenseTag.COMMERCIAL_OK, "EDINET（金融庁）", _CHILD),
    "yuho_overseas_facts": _uniform(LicenseTag.COMMERCIAL_OK, "EDINET（金融庁）", _CHILD),
    # --- stockStock 所有（jss_*）------------------------------------------
    "jss_raw_files": _row_tag(
        "ソース別に混在（EDINET / TDnet / JPX / JSF / Yahoo）",
        "license_tag NOT NULL。本番 150 行で実際に機能している",
    ),
    "jss_financials": _row_tag(
        "EDINET（commercial-ok）と TDnet 短信（factual-cite）が行ごとに違う",
        "license_tag NOT NULL。本番 0 行（writer 未実装）だが列は存在する",
    ),
    "jss_xbrl_documents": _row_tag(
        "EDINET / TDnet", "license_tag NOT NULL。本番 0 行"
    ),
    "jss_supply_latest": _row_tag(
        "日証金（JSF）", "license_tag NOT NULL DEFAULT 'personal-only'。本番 4,351 行"
    ),
    "jss_index_symbols": _row_tag("Yahoo Finance", "license_tag NOT NULL。本番 6 行"),
    # 勘定科目の語彙表。EDINET/TDnet のタクソノミ要素名だけを持ち、値を持たない。
    "jss_xbrl_elements": _uniform(
        LicenseTag.COMMERCIAL_OK,
        "EDINET タクソノミの要素名",
        "license_tag 列を持たない語彙表。ファクトの値は 1 行も入らない（設計書 B-4）",
    ),
    "jss_job_runs": _operational("ジョブ実行履歴。第三者由来の値を持たない"),
    # `license_tag` 列を持つが、それは**他のデータセットを説明する値**で
    # この行自身のタグではない。ROW_TAG と取り違えないこと。
    "jss_dataset_freshness": _operational(
        "鮮度の断面。license_tag 列は観測対象データセットの説明で、この行のタグではない"
    ),
    "jss_writer_claims": _operational("列単位の writer 排他の宣言"),
    "jss_column_license": _operational("列単位ライセンス地図そのもの"),
    "jss_notion_pages": _operational(
        "Notion 行 ID の写し（L-20）。page_id とコードだけを持ち値は持たない"
    ),
    # --- 2026-09-13 に `sqlite_master` の読み取りで名前と DDL が判明した 3 表 ---
    # 当初「本番 PRAGMA を読めないので登録できない」としていたが、読み取り専用の
    # カタログ照会で足りた。未登録のままだと「地図に無い表」が恒久的に warning で
    # 出続け、未知の表を failure へ上げる道が塞がる。
    #
    # 2 つの swing_ は他の swing_ と同じ規則（Yahoo 日足の派生 = personal-only）を
    # 当てる。`swing_sector_daily.sector` は JPX の33業種区分そのものなので、
    # `core_stocks.sector` と同じく personal-only でなければならない。
    "swing_market_context": _uniform(
        LicenseTag.PERSONAL_ONLY,
        _YAHOO,
        "本番 DDL に nikkei_close / nikkei_vi / vix / sp500_pct がある"
        "（2026-09-13 sqlite_master 実測）。judgment は kabulab-cf の自作だが"
        "入力が Yahoo なので継承側へ倒す",
    ),
    "swing_sector_daily": _uniform(
        LicenseTag.PERSONAL_ONLY,
        "JPX data_j.xlsx の33業種 × Yahoo 日足の騰落率",
        "本番 DDL に sector / pct_1d / pct_5d がある（2026-09-13 sqlite_master 実測）。"
        "sector は core_stocks.sector と同じ JPX 由来なので personal-only",
    ),
    # 優待ジャンル。`description` は kabulab-cf が自作して公開面に出している
    # （あちらの services/otakara-yutai/src/tests/public-summary-safety.test.ts が
    # `yutai_genres.description` を「自作説明」として扱っている）。ただしジャンル
    # 分類そのものがみんかぶ由来かは未確認なので、**推測で uniform にしない**。
    # 未分類 = 公開しない扱いなので漏れる方向には倒れない（§3-1）。
    "yutai_genres": TableLicense(
        kind=TableKind.UNCLASSIFIED,
        tag=None,
        source="kabulab-cf 所有（ジャンル名・slug・自作説明）",
        evidence="本番 DDL は id/name/slug/description/created_at のみ"
        "（2026-09-13 sqlite_master 実測）。分類の出自が未確認なので未分類で登録",
    ),
}

# 行タグで判定する表は `license_tag` 列を持っていなければ嘘になる。
ROW_TAG_COLUMN = "license_tag"

# 本番（`_cf_KV` を除く）の表数。finmath 2 表（`finmath_price_snapshot` /
# `finmath_daily_ohlcv`）は kabulab-cf drizzle/d1/0012 で DROP 済み（本番
# sqlite_master で不在を確認）。L-20 で `jss_notion_pages` を足して 30 表。
# L-52 で `swing_stock_screening` を引いて 29 表（kabulab-cf 0015 で DROP）。
# K4b で `p_yuho_growth` を足して 30 表（kabulab-cf 0017 で CREATE 予定）。
# 表を足す側も stockStock の地図を先に main へ（B2）。CREATE までの間は地図の
# 余剰分が「未知の表 = warning」で出るだけ（failure ではない）。
OBSERVED_TABLE_COUNT = 30

# 本番 `sqlite_master` から除く名前。SQLite と D1 の内部表。
_INTERNAL_PREFIXES = ("sqlite_", "_cf_", "d1_", "__drizzle")


def is_internal(table: str) -> bool:
    return table.startswith(_INTERNAL_PREFIXES)


# CREATE TABLE の本体で列定義ではなく表制約を導くキーワード。
_CONSTRAINT_HEADS = frozenset(
    {"PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT", "EXCLUDE"}
)


def ddl_columns(sql: str | None) -> list[str]:
    """`sqlite_master.sql` の CREATE TABLE から列名を取り出す。

    30 表に `PRAGMA table_info` を投げると 30 往復かかる。`sqlite_master` は
    1 文で全表の DDL を返し、**SQLite は `ALTER TABLE ADD COLUMN` のときに
    保存済みの CREATE TABLE 文を書き換える**ので、ALTER で足した列もここに出る
    （`core_stocks` の P4a の 12 列で確認できる）。往復を 30 → 1 にするために
    PRAGMA ではなくこちらを読む。

    括弧の対応を数えて最外周の本体だけを見る。型の `NUMERIC(10, 2)` や
    `DEFAULT (unixepoch())` の中のカンマで分割しないため。
    """
    text = " ".join(str(sql or "").split())
    start = text.find("(")
    if start < 0:
        return []
    depth = 0
    body_end = -1
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                body_end = i
                break
    if body_end < 0:
        return []
    body = text[start + 1 : body_end]

    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))

    columns: list[str] = []
    for part in parts:
        tokens = part.strip().split()
        if not tokens:
            continue
        head = tokens[0].strip('`"[]')
        if head.upper() in _CONSTRAINT_HEADS:
            continue
        columns.append(head)
    return columns


# --- writer の排他宣言 (jss_writer_claims) ---------------------------------
#
# ## `column_group` の語彙を先に固定する理由
#
# 本番の既存 4 行はすべて `column_group='all'` / `writer='stockStock'` で、
# 設計書には `'base'` / `'enrich'` という値の**定義が無い**。PK が
# `(dataset, column_group)` なので、後から値を改名すると DELETE + INSERT の
# 破壊的書換になる。だから値を先に決めて契約ファイルへ書く。
#
# | 値 | 意味 |
# |---|---|
# | `all` | その表の全列を 1 writer が書く（分割の必要が無い表） |
# | `base` | 行の作成 + 基本列（INSERT / UPDATE の両方） |
# | `enrich` | 既存行の UPDATE のみ（INSERT / DELETE を発行しない列集合） |
#
# `base` / `enrich` の意味は設計書 §1.4「同一テーブルの複数 writer は列集合が
# 互いに素のときだけ許す」の表と 1:1 である。
#
# ## 宣言する条件 — 今日実際に writer がいる (dataset, column_group) だけ
#
# 「列を足したが writer を入れ忘れて黙って死ぬ」（`estimate_source_url` が
# 8,314 行すべて NULL なのに `estimated_value` は 5,333 行ある）を検出可能に
# しておくため、**writer がいない群は宣言しない**。
#
# ## `core_stocks` は `base = kabulab-cf` / `enrich = stockStock`（2026-09-13）
#
# `core_stocks` の行の作成と既存列（`name` / `market` / `sector` / `is_active` /
# `updated_at`）を書くのは kabulab-cf の `src/cron/universe.ts` だけである。
# ここで `base` を `stockStock` と宣言すると、kabulab-cf 側に同じ照合を入れた
# 瞬間に `universe.ts` が毎回 throw して **JPX 母集団同期が止まる**。だから
# `base` は `kabulab-cf` のまま動かさない。
#
# `enrich` 群（P4a で足した 12 列のうち `instrument_type` を除く 11 列）は、
# `jobs/master_sync.py` が `sector33` を既存行への UPDATE だけで埋め始めたので
# `stockStock` で宣言する。設計どおり同一 PR で (1) `core_stocks/enrich` を
# `stockStock` で足す (2) 充填ジョブを有効にする、の順に入れた。
#
# ## `instrument_type` は `base` に数える（2026-09-13、kabulab-cf #27）
#
# kabulab-cf #27（P4b 第 1 段）から、`universe.ts` が JPX data_j の区分で
# `instrument_type` を書く。行の作成（INSERT）と同じ upsert で書くので
# 「既存行の UPDATE のみ」という `enrich` の定義に合わず、writer も kabulab-cf
# である。だからこの列は `base` に数える。claim の行（`(dataset, column_group)`
# と writer）は変わらないので、本番の `jss_writer_claims` に DELETE は要らない。
# `universe.ts` は `instrument_type` 以外の `enrich` の列（`sector33` など）を
# SET しない（2026-09-13 に kabulab-cf origin/main を読んで確認。照合コード自体は
# kabulab-cf 側にまだ無い）。
#
# 採らなかった案: `instrument_type` のために群を足す。writer が `base` と同じ
# kabulab-cf で INSERT も発行するので `base` と分ける理由が無く、PK の行を
# 増やすほど後の統合が破壊的書換になる。
#
# `enrich` の 11 列のうち今日書くのは `sector33` だけで、残り 10 列は P4b。
# 群を列ごとに割らないのは、PK が `(dataset, column_group)` なので細かく割るほど
# 後の統合が破壊的書換になるからである（writer が別になる列が出たら割る）。
#
# `all` を使わず最初から `base` にしてあるのは、後で `base` / `enrich` へ割る
# ときに `all` 行の DELETE が必要になるのを避けるためである（PK が
# `(dataset, column_group)` なので改名は破壊的書換になる）。

COLUMN_GROUP_ALL = "all"
COLUMN_GROUP_BASE = "base"
COLUMN_GROUP_ENRICH = "enrich"

COLUMN_GROUPS: dict[str, str] = {
    COLUMN_GROUP_ALL: "その表の全列を 1 writer が書く（分割の必要が無い表）",
    COLUMN_GROUP_BASE: "行の作成 + 基本列（INSERT / UPDATE の両方）",
    COLUMN_GROUP_ENRICH: "既存行の UPDATE のみ（INSERT / DELETE を発行しない列集合）",
}

WRITER_STOCKSTOCK = "stockStock"
WRITER_KABULAB = "kabulab-cf"

# 宣言した日。`jss_writer_claims.updated_at` に入れる。
# **実行時刻 (`now`) を入れない。** 入れると再実行のたびに値が進み、
# 「いつ決めた宣言か」が読めなくなる（`jss_dataset_freshness.updated_at` で
# 同じ間違いをすると監視が恒久的に緑になる。設計書 B-8）。
# 宣言を変える PR はこの日付も同時に進めること。
CLAIM_DECLARED_AT = "2026-09-13"


@dataclass(frozen=True)
class WriterClaim:
    """1 つの (dataset, column_group) を誰が書くか。"""

    dataset: str
    column_group: str
    writer: str
    declared: str  # ISO 日付
    note: str


def _claim(dataset: str, group: str, writer: str, note: str) -> WriterClaim:
    return WriterClaim(
        dataset=dataset,
        column_group=group,
        writer=writer,
        declared=CLAIM_DECLARED_AT,
        note=note,
    )


_JSS_NOTE = "stockStock が単独で書く jss_ 表。分割の必要が無い"

WRITER_CLAIMS: tuple[WriterClaim, ...] = tuple(
    [
        _claim(table, COLUMN_GROUP_ALL, WRITER_STOCKSTOCK, _JSS_NOTE)
        for table in sorted(t for t in TABLE_LICENSE if t.startswith("jss_"))
    ]
    + [
        _claim(
            "core_stocks",
            COLUMN_GROUP_BASE,
            WRITER_KABULAB,
            "kabulab-cf src/cron/universe.ts。行の作成と既存列、instrument_type"
            "（kabulab-cf #27 から）。enrich を stockStock が持った後もここは動かさない",
        ),
        _claim(
            "core_stocks",
            COLUMN_GROUP_ENRICH,
            WRITER_STOCKSTOCK,
            "stockStock master_sync が sector33 を既存行の UPDATE だけで埋める"
            "（updated_at は進めない）。instrument_type は base。残りの P4a 列は P4b",
        ),
        _claim(
            "core_stock_financials",
            COLUMN_GROUP_BASE,
            WRITER_KABULAB,
            "kabulab-cf daily.ts。②断面の交代は P5（G-fin-1 の判定後）",
        ),
        _claim(
            "swing_stock_indicators",
            COLUMN_GROUP_BASE,
            WRITER_KABULAB,
            "kabulab-cf daily.ts。テクニカル断面の交代は P7",
        ),
        _claim(
            "ir_disclosures",
            COLUMN_GROUP_BASE,
            WRITER_KABULAB,
            "行の作成は今日 kabulab-cf。設計書 §1.4 は第6波以降 stockStock へ交代",
        ),
        _claim(
            "ir_disclosures",
            COLUMN_GROUP_ENRICH,
            WRITER_KABULAB,
            "classify.ts の 20 タグと pdf_sentiment*。交代後も kabulab-cf が持つ",
        ),
        _claim(
            "yutai_benefits",
            COLUMN_GROUP_BASE,
            WRITER_KABULAB,
            "行の作成は今日 kabulab-cf（2026-06-22 で更新停止中）",
        ),
        _claim(
            "yutai_benefits",
            COLUMN_GROUP_ENRICH,
            WRITER_KABULAB,
            "estimated_value / short_summary / estimate_*。LLM 推定値を消さないため分割",
        ),
    ]
)

# 照合は warn → fail の 2 段リリース。**2026-09-13 から 2 段目（失敗にする）。**
#
# ## ブートストラップ（1 段目から変えていない不変条件）
#
# **claim が無いときに例外で落とす検査を、投入より前に置いてはいけない。** claim を
# 投入するのも書込ジョブなので、claim 行の無い DB に対する最初の実行が必ず異常終了し、
# ブートストラップ不能になる（鶏と卵）。`jobs/license_map._sync_writer_claims` は
# **投入 → 読み直し → 照合** の順で、失敗にするのは照合の結果だけである。したがって
# 2 段目でも、行の無い DB への初回は投入した 19 行を読み直して一致し、成功する。
#
# ## 2 段目で失敗になるもの（= 投入のあとでも残る差分）
#
# - `claim が実表に無い` / `writer が食い違う`: 投入の直後に読み直しているので、
#   **upsert が届いていない**ことを意味する。黙って次回に期待しない。
# - `宣言に無い claim が実表にある`: upsert では消えない（prune しない）。
#   誰かが宣言外の writer を名乗っている（kabulab-cf 側の手作業・REST 直叩き等）
#   ので、宣言へ足すか行を消すかを人が決める。
#
# ## 宣言を外す・改名するときの順序（2 段目で生まれた罠）
#
# `WRITER_CLAIMS` から (dataset, column_group) を外す／改名する PR を入れると、
# prune しないので**旧キーの行が本番に残り、次の ops_check から毎日失敗**する。
# 行を先に消してもだめで、マージ前の旧コードが次の実行で upsert し直す。
# 順序は「PR をマージ → その次の ops_check が走る前に旧キーを DELETE」。
# 間に合わなければ 1 回赤くなって Issue が立ち、DELETE 後の緑で自動的に閉じる。
# 採らなかった案: 宣言外の claim を自動 prune する — 本番行の由来（kabulab-cf の
# 手作業など）を推測で消すことになる（上の「prune しない」と同じ理由）。
#
# ## 2 段目へ上げた根拠
#
# 当初の条件は (1) 既存行の中身が判明 (2) 食い違う行の整理 PR (3) kabulab-cf 側にも
# 同じ照合、の 3 つだった。
#
# - (1)(2): 2026-09-13 の読み取り照会で、既存 4 行は `jss_financials` /
#   `jss_raw_files` / `jss_supply_latest` / `jss_xbrl_documents` の
#   `('all', 'stockStock')` で宣言と完全一致し、整理は不要だった。
# - 1 段目の本番実行（run 34750652448）は「宣言 18 件を投入」で**食い違いの
#   warning が 0 件**。本番 `jss_writer_claims` は 18 行。2 段目へ上げても今日の
#   本番は赤くならない。
# - (3) kabulab-cf 側の照合は**まだ無い**（2026-09-13 に origin/main を
#   `git grep writer_claim` して 0 件）。それでも上げたのは、stockStock 側の照合を
#   失敗にするかどうかは kabulab-cf 側の有無と独立に決められるから（待っても
#   stockStock の検知力が上がるわけではない）。「片側だけの規律にしない」は
#   kabulab-cf 側に照合を入れる別の作業として残っている。
#
# ## 定数を消さずに True で残す理由
#
# 本番で予期しない赤が出たときの戻し方を 1 行の変更にしておくため。
# 採らなかった案: 定数と warning 分岐を削除する — 戻すときに分岐を書き直す
# ことになり、急いで戻す場面で差分が大きくなる。
CLAIM_MISMATCH_IS_FAILURE = True

WRITER_CLAIMS_SQL = "SELECT dataset, column_group, writer FROM jss_writer_claims"


def declared_epoch(iso_date: str) -> int:
    """宣言日を epoch 秒にする。**UTC 固定で決定的にする。**

    ローカルタイムゾーンに依存させると、同じ宣言が実行環境（CI は UTC、手元は
    JST）によって別の値になり、投入が毎回 UPDATE を打つ = 冪等でなくなる。
    claim の `updated_at` は「いつ決めた宣言か」の粗い記録で、日付の境界で
    何かを判定する列ではないので時刻は 00:00 UTC で足りる。
    """
    year, month, day = (int(x) for x in iso_date.split("-"))
    return int(datetime(year, month, day, tzinfo=UTC).timestamp())


def writer_claim_rows() -> list[list[object]]:
    """`jss_writer_claims` へ投入する行。"""
    return [
        [c.dataset, c.column_group, c.writer, declared_epoch(c.declared)]
        for c in WRITER_CLAIMS
    ]


def writer_claims_need_seed(observed_rows: list[dict]) -> bool:
    """宣言にあって実表に無い・食い違う claim があれば True。

    日次ジョブは True のときだけ upsert を打つ（L-17）。宣言に無い claim
    （prune しない方針）は投入では直らないので対象外。
    """
    declared = {(c.dataset, c.column_group): c.writer for c in WRITER_CLAIMS}
    observed = {
        (str(r.get("dataset") or ""), str(r.get("column_group") or "")): str(
            r.get("writer") or ""
        )
        for r in observed_rows
    }
    return any(
        key not in observed or observed[key] != writer
        for key, writer in declared.items()
    )


def writer_claim_problems(observed_rows: list[dict]) -> tuple[list[str], list[str]]:
    """実表の claim を宣言と突き合わせる。`(failures, warnings)` を返す。

    `CLAIM_MISMATCH_IS_FAILURE` が True（2 段目。2026-09-13 から）の間は
    すべて failure になる。False に戻すとすべて warning になる（1 段目）。
    **投入のあとに呼ぶこと**（前に呼ぶと、行の無い DB への初回が必ず失敗する）。
    """
    declared = {(c.dataset, c.column_group): c.writer for c in WRITER_CLAIMS}
    observed = {
        (str(r.get("dataset") or ""), str(r.get("column_group") or "")): str(
            r.get("writer") or ""
        )
        for r in observed_rows
    }
    messages: list[str] = []
    for key in sorted(declared):
        if key not in observed:
            messages.append(f"claim が実表に無い: {key} -> {declared[key]}")
        elif observed[key] != declared[key]:
            messages.append(
                f"claim の writer が食い違う: {key} 実表 {observed[key]!r} /"
                f" 宣言 {declared[key]!r}"
            )
    for key in sorted(set(observed) - set(declared)):
        # **prune しない。** 本番の既存 4 行は本レーンからは読めず、推測で消すと
        # 読めない情報を壊す。名前を出して整理 PR を促す。
        message = f"宣言に無い claim が実表にある: {key} -> {observed[key]!r}（prune しない）"
        dataset = key[0]
        if key[1] == COLUMN_GROUP_ALL and any(
            d == dataset and g != COLUMN_GROUP_ALL for d, g in declared
        ):
            message += (
                f"。{dataset} は列群を分けて宣言しているので、この 'all' 行は"
                " 同じ列を別 writer が持つと主張していることになる（整理が必要）"
            )
        messages.append(message)

    if CLAIM_MISMATCH_IS_FAILURE:
        return messages, []
    return [], messages


@dataclass(frozen=True)
class CoverageReport:
    """地図の網羅性の検査結果。行を 1 行も走査せずに作れるものだけを持つ。"""

    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    tables: int = 0
    columns: int = 0


# `sqlite_master` から全表の DDL を 1 文で取る。**行を走査しない**
# （スキーマのカタログで、実データの表には触れない）。30 表に `PRAGMA
# table_info` を投げる案との差は往復 30 回 → 1 回。
ALL_TABLES_SQL = (
    "SELECT name, sql FROM sqlite_master WHERE type='table'"
    " AND name NOT LIKE 'sqlite_%' ORDER BY name"
)


def coverage(observed: dict[str, str | None]) -> CoverageReport:
    """本番の (表, 列) が地図に載っているかを見る。

    `observed` は表名 → `sqlite_master.sql`。**値（行）は一切見ない。**

    fail open / closed の境界は module docstring の「スナップショットの所有と
    更新手順」に書いてある。要点は「宣言に無い表があった = warning、宣言にある
    表が消えた・行タグ前提の表に列が無い = failure」。
    """
    from .schema import MIXED_LICENSE_COLUMNS

    failures: list[str] = []
    warnings: list[str] = []

    live = {name: ddl for name, ddl in observed.items() if not is_internal(name)}
    columns_by_table = {name: ddl_columns(ddl) for name, ddl in live.items()}
    total_columns = sum(len(c) for c in columns_by_table.values())

    unknown = sorted(set(live) - set(TABLE_LICENSE))
    if unknown:
        # fail open。対向リポジトリの migration ごとに毎日赤くする検査は
        # 読まれなくなる。名前を出して追随 PR を促す側に倒す。
        warnings.append(
            f"地図に無い表が本番にある: {unknown}"
            "（`cloud_store/governance.TABLE_LICENSE` へ足すこと。タグが未決なら"
            " TableKind.UNCLASSIFIED で登録してよい。宣言が無い表の列は"
            "「公開してよいと決まっていない」= 公開しない扱いになる）"
        )

    vanished = sorted(set(TABLE_LICENSE) - set(live))
    if vanished:
        # fail closed。宣言が実体から外れたまま残るのが「2 つの真実」の始まり。
        failures.append(
            f"地図にあって本番に無い表: {vanished}"
            "（移行で消したなら宣言も同じ PR で外す。推測で残さない）"
        )

    unclassified = sorted(
        name for name, spec in TABLE_LICENSE.items()
        if spec.kind is TableKind.UNCLASSIFIED and name in live
    )
    if unclassified:
        warnings.append(f"タグ未決のまま登録されている表: {unclassified}")

    # 列地図を持つ表は 2 つの宣言が食い違ってはいけない。
    mapped_tables = set(MIXED_LICENSE_COLUMNS)
    column_map_tables = {
        name for name, spec in TABLE_LICENSE.items() if spec.kind is TableKind.COLUMN_MAP
    }
    if mapped_tables != column_map_tables:
        failures.append(
            "列地図 (schema.MIXED_LICENSE_COLUMNS) と表区分 (TableKind.COLUMN_MAP) が"
            f" 食い違う: 列地図={sorted(mapped_tables)} 表区分={sorted(column_map_tables)}"
        )

    for name, spec in sorted(TABLE_LICENSE.items()):
        if name not in live:
            continue  # vanished で既に報告済み
        columns = set(columns_by_table[name])
        if spec.kind is TableKind.ROW_TAG and ROW_TAG_COLUMN not in columns:
            failures.append(
                f"{name}: 行タグで判定する宣言なのに {ROW_TAG_COLUMN} 列が無い"
                "（行タグ前提が成立していない。本番 8 表のうち行が入って機能して"
                "いたのは 3 表だけだったのと同じ穴）"
            )
        if spec.kind is not TableKind.COLUMN_MAP:
            continue
        declared = set(MIXED_LICENSE_COLUMNS.get(name, {}))
        missing = sorted(declared - columns)
        if missing:
            failures.append(
                f"{name}: 列地図にあって本番に無い列 {missing}"
                "（列を消したなら地図も同じ PR で直す）"
            )
        undeclared = sorted(columns - declared)
        if undeclared:
            # fail open だが実効は fail closed: 宣言が無い列は公開投影に
            # 入らないので、漏れる方向へは倒れない。決まっていないことを
            # 見えるようにするための報告である。
            warnings.append(
                f"{name}: タグ未宣言の列 {undeclared}"
                "（未宣言 = 公開してよいと決まっていない = 公開しない。"
                " 1 列ずつ決めて schema.MIXED_LICENSE_COLUMNS へ足す）"
            )

    return CoverageReport(
        failures=tuple(failures),
        warnings=tuple(warnings),
        tables=len(live),
        columns=total_columns,
    )


__all__ = [
    "ALL_TABLES_SQL",
    "CLAIM_DECLARED_AT",
    "CLAIM_MISMATCH_IS_FAILURE",
    "COLUMN_GROUPS",
    "WRITER_CLAIMS",
    "WRITER_CLAIMS_SQL",
    "WriterClaim",
    "declared_epoch",
    "writer_claim_problems",
    "writer_claim_rows",
    "writer_claims_need_seed",
    "OBSERVED_TABLE_COUNT",
    "ROW_TAG_COLUMN",
    "TABLE_LICENSE",
    "CoverageReport",
    "TableKind",
    "TableLicense",
    "coverage",
    "ddl_columns",
    "is_internal",
]
