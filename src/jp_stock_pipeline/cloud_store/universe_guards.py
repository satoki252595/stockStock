"""①銘柄マスタの母集団ガード（移行 P4a）。

kabulab-cf の `src/cron/universe.ts` の `assertUniverseCoverage`（:82-120、定数は
:55/:57/:59/:61）からの移植。writer を stockStock へ移す前に、**同じ条件で同じ
ように止まる**ことを先に固定しておくためのモジュール。

設計書 `docs/CF-CANONICAL-DESIGN.md` は「`universe.ts:155-201` の3段ガード」と
書いているが、実コードは次の2点で異なる（2026-09-12 に実測して訂正済み）。

- 実体は `scripts/sync/universe.ts`（29行の薄い CLI）ではなく `src/cron/universe.ts`
- 「3段」ではなく **4条件**。設計書 §12 の (a)-(d) の列挙のほうが実コードと一致する

**比較の向きを移植元と厳密に合わせること。** (a)(b)(c) は「未満で失敗」、(d) は
「超過で失敗」。境界値ちょうどは通る。

**(c)(d) は `existing_active_count == 0` のとき丸ごとスキップされる。** これは
0 除算避けではなく初回 seed を通すための意図的な穴で、移植元の挙動をそのまま保つ。
SELECT の失敗を「テーブルが空」と取り違えてガードを無効化しないよう、
`assert_population_sane()` を別に用意している。

## 移植元から意図的に変えた点: (c) の分母（D4）

移植元は (c)(d) の分母をどちらも `is_active=1` の**全件**にしている。P4b で
ETF/ETN/PRO/外国株が +725 行入って active が 4,440 になると、(c) は
**3,700/4,440 = 0.833 < 0.98** で毎月 throw し、母集団同期が恒久的に止まる
（`docs/CF-CANONICAL-DESIGN.md` P4b 節の実測）。分子 `equity_count` は
`isListedEquity` を通った内国普通株しか数えないのに、分母だけが全銘柄種別を
数えているせいで、**母集団を広げるほど比率が下がる**という壊れ方をする。

→ **(c) の分母だけを `is_active=1 AND instrument_type='equity'` に絞る。**

### (c) と (d) で分母を別に持つ理由（採った案）

(d) の分子 `pending_deactivation_count` は active **全件**から算出される
（`should_deactivate_universe_code` は data_j の全行集合と突き合わせるので、
ETF や REIT の上場廃止も候補に入る）。ここで (d) の分母だけを equity に絞ると
**分子 ⊄ 分母**になり、「守っている母集団に対する割合」という意味が消える。
極端には ETF が 200 本廃止されただけで 200/3,715 ではなく 200/2,990 のような
別母集団の比率になり、比率が 1 を超えることすらある。よって:

- **(c) = equity の分母**（分子が equity なので合わせる）
- **(d1) = active 全体の分母**（分子が active 全体なので合わせる。移植元と同じ）
- **(d2) = equity の分母 × equity の分子**（新設。下記）

採らなかった案 2 つと、採らなかった理由:

1. **(c)(d) 両方を equity の分母にする**（設計書が当初書いていた案）
   → (d) が上記の非可換な比率になる。ETF/ETN/PRO の上場廃止が月 74 件を超えれば
   (d) が誤発火して、D4 が直そうとした「毎月止まる」が形を変えて残る。**却下。**
2. **(d) を移植元のまま（active 全体）据え置く**
   → 分子・分母は可換だが、P4b で active が 3,715 → 4,440 に膨らむと一括対象外化の
   上限が **74 件 → 88 件**へ自動的に緩む。対象外化の候補は実質すべて内国普通株
   なので、防御だけが弱くなる。**(d1) として残すが、これ単独では不足。**

→ (d) を **2 本**にした。(d1) は「銘柄種別を問わない大量対象外化」（ETF が一斉に
消える事故）を拾い、(d2) は「内国普通株の大量対象外化」を equity 内の比率で拾う。
どちらも分子と分母が同じ母集団なので、比率としての意味が壊れない。

## `instrument_type` が全 NULL である遷移期の扱い

2026-09-12 時点の本番 `core_stocks` は P4a の列追加だけが済んでおり、
**`instrument_type` は 3,818 行すべて NULL**（充填は P4a の範囲外。設計書
P4a 実施記録のとおり語彙が未決）。この状態で equity に絞ると分子ではなく
**分母が 0** になる。

→ `existing_equity_active_count` が `None` または `0` のときは
**「未充填」と判断して従来の分母（active 全体）へ落とす**。理由:

- 充填前の active 3,715 件は集合として内国普通株とほぼ一致する（ETF/ETN/PRO/
  外国株は P4b の INSERT でこれから入る +725 行の側にある）。したがって充填前は
  従来の分母が equity の分母の良い近似であり、ガードの意味が変わらない
- NULL を `equity` とみなす（= 分母を全 active と等価に扱う）実装にすると、
  P4b で ETF が入ってからも NULL のまま増え続けた場合に**気づけない**。
  「未充填なら従来の分母」という明示的な縮退のほうが、後で外しやすい

**この縮退は fail-closed である。** 充填せずに P4b を実行すると active が 4,440 に
なっても分母は active 全体のままなので、(c) は 0.833 で発火して止まる。これは
誤検知ではなく「充填は P4b の前提条件」という設計の表明で、
`assert_instrument_type_backfilled()` が同じことを先に、読める言葉で止める。

## `instrument_type='equity'` の充填述語をずらしてはいけない

(c) の分子は `isListedEquity`（「内国株式」かつ プライム|スタンダード|グロース
かつ 4文字コード）を通った件数。分母を `instrument_type='equity'` で数えるなら、
**充填も同じ述語でなければならない**。たとえば PRO Market の内国株を `equity` に
入れると、分母が分子より構造的に大きくなって (c) が恒久的に 0.98 を割る。

## 対向（kabulab-cf 側）の改修

実際に月次で throw するのは kabulab-cf の `src/cron/universe.ts` であって、この
モジュールではない（stockStock 側にはまだ呼び出し元が無い）。**別リポジトリなので
ここからは触らない。** 必要な対向改修は
`docs/CF-CANONICAL-DESIGN.md` の「対向改修（kabulab-cf 側）」に書いた。
"""

from __future__ import annotations

import re

from .guards import GuardError

# --- 移植元の定数（universe.ts の行番号を併記。変更時は両方を見ること）---------
MIN_JPX_ROWS = 4_000  # universe.ts:55  MIN_JPX_ROWS
MIN_EQUITY_ROWS = 3_000  # universe.ts:57  MIN_EQUITY_ROWS
MIN_EXISTING_COVERAGE = 0.98  # universe.ts:59  MIN_EXISTING_COVERAGE
MAX_DEACTIVATION_RATIO = 0.02  # universe.ts:61  MAX_DEACTIVATION_RATIO

# `core_stocks.instrument_type` の内国普通株を表す語彙。(c) の分母の絞り込み条件
# `is_active = 1 AND instrument_type = 'equity'` と、充填側で同じ文字列を使う。
INSTRUMENT_TYPE_EQUITY = "equity"

# JPX の4文字コード契約（英字入り新方式コード "130A" を含む）。
# kabulab-cf の src/shared/jpx/stock-code.ts:31 と同じ。
STOCK_CODE_RE = re.compile(r"^\d{3}[0-9A-Z]$")


def normalize_stock_code(code: str | None) -> str:
    """移植元 `src/shared/jpx/stock-code.ts:56-63` の `normalizeStockCode` と同じ。

    trim → **全角英数字を半角へ** → 大文字化。全角化は data_j 以外の入力
    （手入力・別ソース）が混ざったときに判定が割れないようにするためで、
    移植元にある以上こちらでも落とさない。
    """
    text = str(code or "").strip()
    # 全角英数字 U+FF10-FF19 / U+FF21-FF3A / U+FF41-FF5A を半角へ (-0xFEE0)
    half = "".join(
        chr(ord(ch) - 0xFEE0) if "\uff10" <= ch <= "\uff5a" else ch for ch in text
    )
    return half.upper()


def is_valid_stock_code(code: str | None) -> bool:
    """4文字コード契約に合格するか。5桁の種類株はここで落ちる。"""
    return bool(STOCK_CODE_RE.match(normalize_stock_code(code)))


def coverage_denominator(
    existing_active_count: int,
    existing_equity_active_count: int | None,
) -> tuple[int, str]:
    """(c) の分母と、それを選んだ理由のラベルを返す。

    ラベルはエラーメッセージへ入れる。「0.833 で落ちた」だけを見せられても、
    分母が equity なのか active 全体なのか（= 充填漏れなのか本当の被覆不足
    なのか）が読めず、対処が分かれてしまう。
    """
    if existing_equity_active_count is None:
        return existing_active_count, "active 全体／instrument_type 未観測"
    if existing_equity_active_count <= 0:
        # 列はあるが全 NULL（2026-09-12 の本番はこの状態）。上の docstring の
        # 遷移期の扱いのとおり従来の分母へ縮退する。fail-closed。
        return existing_active_count, "active 全体／instrument_type 未充填"
    return existing_equity_active_count, f"active かつ {INSTRUMENT_TYPE_EQUITY}"


def assert_universe_coverage(
    raw_count: int,
    equity_count: int,
    existing_active_count: int,
    pending_deactivation_count: int,
    *,
    existing_equity_active_count: int | None = None,
    pending_deactivation_equity_count: int | None = None,
) -> None:
    """母集団の条件。1つでも破れたら書込ゼロで止める（universe.ts:82-120 + D4）。

    :param raw_count: JPX data_j の**全行数**。ETF/REIT/PRO/外国株を含む
        （内国株に絞る前の値）。universe.ts:159 の第1引数。
    :param equity_count: `isListedEquity` 相当を通った行数。「内国株式」かつ
        プライム|スタンダード|グロース かつ4文字コード契約、の3条件すべて。
    :param existing_active_count: `core_stocks` の **is_active=1 の行数のみ**。
        total ではない（取り違えると (d1) の分母が狂う）。
    :param pending_deactivation_count: これから is_active=0 にする件数（全銘柄種別）。
    :param existing_equity_active_count: `is_active=1 AND instrument_type='equity'`
        の行数。(c) の分母および (d2) の分母。`None`／`0` は「未観測・未充填」で、
        (c) は従来の分母へ縮退し (d2) は評価されない（docstring の遷移期の節）。
    :param pending_deactivation_equity_count: 対象外化候補のうち
        `instrument_type='equity'` の件数。(d2) の分子。`None` なら (d2) は評価しない。
    """
    # (a) universe.ts:88-93
    if raw_count < MIN_JPX_ROWS:
        raise GuardError(
            f"ガード(a): JPX listing が {raw_count} 件で安全下限 {MIN_JPX_ROWS} 件未満"
        )
    # (b) universe.ts:94-99
    if equity_count < MIN_EQUITY_ROWS:
        raise GuardError(
            f"ガード(b): JPX 対象株が {equity_count} 件で安全下限 {MIN_EQUITY_ROWS} 件未満"
        )
    # (c) universe.ts:100-109 — existing==0 は移植元どおりスキップする。
    # 分母だけ移植元から変えてある（D4。理由はモジュール docstring）。
    denominator, denominator_label = coverage_denominator(
        existing_active_count, existing_equity_active_count
    )
    if denominator > 0 and equity_count / denominator < MIN_EXISTING_COVERAGE:
        raise GuardError(
            f"ガード(c): JPX 対象株 {equity_count} 件が既存 {denominator} 件"
            f"（{denominator_label}）の {MIN_EXISTING_COVERAGE:.0%} 未満"
        )
    # (d1) universe.ts:110-119 — 分子が全銘柄種別なので分母も active 全体で据え置く。
    if (
        existing_active_count > 0
        and pending_deactivation_count / existing_active_count > MAX_DEACTIVATION_RATIO
    ):
        raise GuardError(
            f"ガード(d1): 対象外化候補 {pending_deactivation_count} 件が既存 active "
            f"{existing_active_count} 件の {MAX_DEACTIVATION_RATIO:.0%} 超"
        )
    # (d2) 新設。equity の分子を equity の分母で見る。(d1) だけだと P4b 後に
    # 内国普通株の対象外化の実効上限が 74 -> 88 件へ緩むのを塞ぐ。
    if (
        pending_deactivation_equity_count is not None
        and existing_equity_active_count is not None
        and existing_equity_active_count > 0
        and pending_deactivation_equity_count / existing_equity_active_count
        > MAX_DEACTIVATION_RATIO
    ):
        raise GuardError(
            f"ガード(d2): 内国普通株の対象外化候補 "
            f"{pending_deactivation_equity_count} 件が既存 active かつ "
            f"{INSTRUMENT_TYPE_EQUITY} {existing_equity_active_count} 件の "
            f"{MAX_DEACTIVATION_RATIO:.0%} 超"
        )


def should_deactivate_universe_code(code: str, raw_codes: frozenset[str]) -> bool:
    """対象外化の判定（universe.ts:123-128）。

    `raw_codes` は `isListedEquity` で絞る**前**の data_j 全行のコード集合。
    ここを内国株だけに絞ると ETF や REIT が毎回対象外化されて (d1) が発火する。
    """
    return code not in raw_codes or not is_valid_stock_code(code)


def assert_population_sane(total_count: int, active_count: int) -> None:
    """移植元に無い追加ガード。(c)(d) の `existing==0` スキップの悪用を防ぐ。

    SELECT が 0 件を返した（通信失敗・条件ミス）のと、本当にテーブルが空なのは
    区別できない。行はあるのに active が 0 なら前者を疑って止める。
    """
    if active_count == 0 and total_count > 0:
        raise GuardError(
            f"core_stocks は {total_count} 行あるのに active が 0 件。"
            " SELECT の失敗を疑う（ガード(c)(d)を無効化させない）"
        )


def assert_instrument_type_backfilled(
    active_count: int, equity_active_count: int
) -> None:
    """P4b（母集団 +725 行）の前提条件。`instrument_type` の充填を確かめる。

    未充填のまま P4b を実行しても (c) は従来の分母で 0.833 になって発火するので
    事故そのものは起きない。しかしそのとき出るのは「被覆率が足りない」という
    **症状の**メッセージで、原因（充填していない）が読めない。P4b の実行前に
    これを先に呼んで、原因の言葉で止める。
    """
    if active_count > 0 and equity_active_count == 0:
        raise GuardError(
            f"core_stocks の active は {active_count} 件だが instrument_type="
            f"'{INSTRUMENT_TYPE_EQUITY}' が 0 件。P4b（母集団拡張）の前提である"
            " instrument_type の充填が済んでいない"
            "（未充填のままだとガード(c)は active 全体の分母へ縮退する）"
        )
