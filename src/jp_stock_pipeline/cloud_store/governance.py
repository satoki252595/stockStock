"""D1 の表ごとのライセンス区分（地図の第2枚目ではなく、第1枚目の続き）。

`schema.MIXED_LICENSE_COLUMNS` は **1 行にライセンスが混在する表**だけを列単位で
持つ。ここはその外側、つまり「この表全体はどの区分か」を持つ。2 つで 1 枚の地図
であり、どちらにも載っていない (表, 列) が無いことを `jobs/license_map.py` が
毎日確かめる。

## 何が穴だったか

本番 30 表 374 列のうち、行タグも列地図も無いのが **22 表 / 256 列（68.4%）**
だった。`license_tag` 列を物理的に持つ表は 8 つあるが、**行が入って実際に
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

- **所有**: この宣言は stockStock が持つ。理由は、kabulab-cf には**テストを回す
  CI が存在しない**（3 workflow はすべて schedule/dispatch のデータ取込）ので、
  乖離を検出できる場所が物理的にこちら側にしか無い。
- **更新手順**: `jobs/license_map.py` が本番 `sqlite_master` を読んで「宣言に
  無い表」を名前つきで報告する。報告された表を `TABLE_LICENSE` へ足す PR を
  出す。タグが分からなければ `UNCLASSIFIED` で登録してよい（「未分類だと
  分かっている」は「宣言が無い」より強い）。
- **fail open / closed**: 「宣言に無い表が本番にある」は **warning**（fail
  open）。「宣言にある表が本番から消えた」「行タグ前提の表に `license_tag` 列が
  無い」「列地図にある列が本番から消えた」は **failure**（fail closed）。

  未知の表を即 failure にしない理由は 2 つある。(1) この宣言は本番 PRAGMA を
  読めない環境（レビュー時・移行期）で書き起こしたもので、30 表のうち名前まで
  裏付けが取れたのは 27 表である。残りを推測で埋めれば「宣言にある表が本番に
  無い」側で毎日赤くなる（`§3-1` 推測しない）。(2) 対向リポジトリの migration の
  たびに毎日赤くなる検査は読まれなくなり、通知を殺すのと同じになる（設計書
  §7.6 を自分自身に適用する）。**最初の本番実行が残り 3 表の名前を出す**ので、
  それを登録する追随 PR で failure へ上げる。
"""

from __future__ import annotations

from dataclasses import dataclass
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
# 統合監査の実測表。
_AUDIT = "docs/CLOUDFLARE-CONSOLIDATION.md §1（2026-09-11 本番実測の行数つき）"

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
    "swing_stock_screening": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _CHILD),
    "rsi_percentile": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _CHILD),
    "otakara_stock_financials": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _CHILD),
    "otakara_stock_scores": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _CHILD),
    "finmath_price_snapshot": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _AUDIT),
    "finmath_daily_ohlcv": _uniform(LicenseTag.PERSONAL_ONLY, _YAHOO, _AUDIT),
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
}

# 行タグで判定する表は `license_tag` 列を持っていなければ嘘になる。
ROW_TAG_COLUMN = "license_tag"

# 宣言の裏付けが取れている表の数。本番は 30 表なので残り 3 表は
# 最初の本番実行が名前を出す（module docstring の「更新手順」）。
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


@dataclass(frozen=True)
class CoverageReport:
    """地図の網羅性の検査結果。行を 1 行も走査せずに作れるものだけを持つ。"""

    failures: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    tables: int = 0
    columns: int = 0

    @property
    def clean(self) -> bool:
        return not self.failures


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
