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

### 「未充填」は 0 件だけではない（部分充填）

2026-09-12 のレビューで見つかった穴。**`None` と `0` だけを未充填として扱うと、
充填が途中で止まった状態で (c) が黙って空虚になる。** 充填は 3,818 行への
`UPDATE` をチャンクで回す別フェーズなので、D1 のレート制限やタイムアウトで
途中終了しうる（語彙が未決なので一部の区分だけ先に埋める運用もありうる）。

equity 件数が 500 のまま P4b を通すと:

- **(c) は fail-open**: 3,100/500 = 6.2 なので 0.98 を割らない。本当の被覆率は
  3,100/4,440 = 0.698 で、部分取得された data_j を素通しする
- **(d2) は fail-closed だが誤発火**: 11/500 = 2.2% で止まる。実母集団に対しては
  11/3,700 = 0.3% で、これは D4 が消したはずの「毎月 throw する」の再来

→ **`MIN_BACKFILLED_EQUITY_ROWS`（= `MIN_EQUITY_ROWS` = 3,000）を下回る equity
件数は「部分充填」として `0` と同じ扱いにする。** (c) は従来の分母へ縮退し
（P4b 後なら 0.833 で発火 = fail-closed）、(d2) は評価しない（誤発火させない）。
どちらも「充填が終わっていないなら equity の分母を信用しない」という 1 つの
述語（`instrument_type_backfilled()`）から出す。

**equity 件数が active 件数を超える入力は拒否する。** equity active ⊆ active なので
構造的にありえない。起きたとすれば引数の取り違えか SELECT の失敗で、その値を
分母に使うと (c)(d2) の意味が丸ごと変わる（`assert_population_sane()` と同じ趣旨）。

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

from ..contracts.stock_code import (
    STOCK_CODE_RE,
    is_valid_stock_code,
    normalize_stock_code,
)
from .guards import GuardError

# --- 移植元の定数（universe.ts の行番号を併記。変更時は両方を見ること）---------
MIN_JPX_ROWS = 4_000  # universe.ts:55  MIN_JPX_ROWS
MIN_EQUITY_ROWS = 3_000  # universe.ts:57  MIN_EQUITY_ROWS
MIN_EXISTING_COVERAGE = 0.98  # universe.ts:59  MIN_EXISTING_COVERAGE
MAX_DEACTIVATION_RATIO = 0.02  # universe.ts:61  MAX_DEACTIVATION_RATIO

# `core_stocks.instrument_type` の内国普通株を表す語彙。(c) の分母の絞り込み条件
# `is_active = 1 AND instrument_type = 'equity'` と、充填側で同じ文字列を使う。
INSTRUMENT_TYPE_EQUITY = "equity"

# 「充填が済んでいる」と見なす equity 件数の下限。**新しい数字を発明していない**:
# (b) が「内国株式が 3,000 件未満の入力は部分取得として拒否する」と既に宣言して
# いるので、`core_stocks` 側の内国普通株が 3,000 件を割っているなら、それは
# 母集団が壊れているか充填が途中で止まっているかのどちらかしかない。
#
# この下限が無いと **(c) が黙って空虚になる**（下の「部分充填」の節）。
MIN_BACKFILLED_EQUITY_ROWS = MIN_EQUITY_ROWS

# JPX の4文字コード契約は `contracts/stock_code.py` が正準実装を持つ。
# ここに同じ正規表現と正規化を写経していたため、TDnet/EDINET/reconcile の各
# 実装と少しずつ答えが割れていた（実測で 13 入力のうち 7 入力が不一致）。
# 後方互換のため名前はここからも見えるようにしておく（既存の import 元を壊さない）。
__all__ = [
    "INSTRUMENT_TYPE_EQUITY",
    "MIN_BACKFILLED_EQUITY_ROWS",
    "STOCK_CODE_RE",
    "normalize_stock_code",
    "is_valid_stock_code",
    "MIN_JPX_ROWS",
    "MIN_EQUITY_ROWS",
    "MIN_EXISTING_COVERAGE",
    "MAX_DEACTIVATION_RATIO",
    "assert_universe_coverage",
    "should_deactivate_universe_code",
    "assert_population_sane",
]


def instrument_type_backfilled(existing_equity_active_count: int | None) -> bool:
    """equity の分母を信用してよいか。(c) の縮退と (d2) の評価が同じ述語を使う。

    ここを「`None`/`0` 以外なら信用する」にすると、充填が途中で止まった状態で
    (c) が空虚になり (d2) が誤発火する（モジュール docstring「部分充填」の節）。
    """
    return (
        existing_equity_active_count is not None
        and existing_equity_active_count >= MIN_BACKFILLED_EQUITY_ROWS
    )


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
    if not instrument_type_backfilled(existing_equity_active_count):
        # 充填が途中で止まっている。この分母を使うと (c) が空虚になるので、
        # 0 件とまったく同じに扱う（縮退先は従来の分母 = fail-closed）。
        return (
            existing_active_count,
            f"active 全体／instrument_type 部分充填"
            f"（equity {existing_equity_active_count} 件 <"
            f" 下限 {MIN_BACKFILLED_EQUITY_ROWS} 件）",
        )
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

    :param raw_count: JPX data_j の**全行数**。ETF/REIT/PRO/外国株を含み、
        5文字の種類株行も含む（内国株にも4文字コード契約にも絞る**前**の値）。
        universe.ts:159 の第1引数 = `downloadJpxListing()` が返した配列の長さ。

        **パーサ側で非正準コードの行を落としてはならない。** 落とすと
        raw_count が「正準コード行数」に変質し、MIN_JPX_ROWS=4000 が
        「部分取得・列崩れの検知器」として機能しなくなる（ETF/REIT が
        丸ごと消えても 4,000 を割らなければ通ってしまう）。母集団の
        絞り込みは equity_count 側（`isListedEquity` 相当）の責務。
    :param equity_count: `isListedEquity` 相当を通った行数。「内国株式」かつ
        プライム|スタンダード|グロース かつ4文字コード契約、の3条件すべて。
    :param existing_active_count: `core_stocks` の **is_active=1 の行数のみ**。
        total ではない（取り違えると (d1) の分母が狂う）。
    :param pending_deactivation_count: これから is_active=0 にする件数（全銘柄種別）。
    :param existing_equity_active_count: `is_active=1 AND instrument_type='equity'`
        の行数。(c) の分母および (d2) の分母。`None`／`0`／
        `MIN_BACKFILLED_EQUITY_ROWS` 未満は「未観測・未充填・部分充填」で、
        (c) は従来の分母へ縮退し (d2) は評価されない（docstring の遷移期・
        部分充填の節）。`existing_active_count` を超える値は拒否する。
    :param pending_deactivation_equity_count: 対象外化候補のうち
        `instrument_type='equity'` の件数。(d2) の分子。`None` なら (d2) は評価しない。
    """
    # equity active ⊆ active なので超過はありえない。引数の取り違えか SELECT の
    # 失敗で、その値を分母に使うと (c)(d2) の意味が丸ごと変わる。先に止める。
    if (
        existing_equity_active_count is not None
        and existing_equity_active_count > existing_active_count
    ):
        raise GuardError(
            f"ガード(前提): active かつ {INSTRUMENT_TYPE_EQUITY} が"
            f" {existing_equity_active_count} 件で active 全体"
            f" {existing_active_count} 件を超えている。部分集合なのでありえない。"
            " 引数の取り違えか SELECT の失敗を疑う"
        )
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
    # 充填が終わっていない equity 件数を分母にすると、実母集団に対して 0.3% の
    # 対象外化が 2.2% に見えて誤発火する（= D4 が消したはずの「毎月 throw」）。
    # (c) の縮退と同じ述語で、信用できないときは評価しない。
    if (
        pending_deactivation_equity_count is not None
        and instrument_type_backfilled(existing_equity_active_count)
        and existing_equity_active_count is not None
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

    **5文字コードも raw_codes に残っていること**が前提。パーサ側で落とすと
    `code not in raw_codes` 節が種類株について到達不能になる（今日は後段の
    `not is_valid_stock_code` 節が先に効くので結果は変わらないが、片方の節が
    死んでいることに気づけなくなる）。
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

    **0 件だけでなく部分充填も止める。** 充填は 3,818 行への UPDATE をチャンクで
    回すので途中終了しうる。3,000 件（`MIN_BACKFILLED_EQUITY_ROWS`）を下回る
    equity 件数を「充填済み」と認めると (c) が空虚になる（モジュール docstring
    「部分充填」の節）。
    """
    if equity_active_count > active_count:
        raise GuardError(
            f"instrument_type='{INSTRUMENT_TYPE_EQUITY}' が {equity_active_count} 件で"
            f" active 全体 {active_count} 件を超えている。部分集合なのでありえない。"
            " 引数の取り違えか SELECT の失敗を疑う"
        )
    if active_count > 0 and not instrument_type_backfilled(equity_active_count):
        detail = (
            "が 0 件"
            if equity_active_count == 0
            else f"が {equity_active_count} 件しかなく下限"
            f" {MIN_BACKFILLED_EQUITY_ROWS} 件を下回る（充填が途中で止まっている）"
        )
        raise GuardError(
            f"core_stocks の active は {active_count} 件だが instrument_type="
            f"'{INSTRUMENT_TYPE_EQUITY}' {detail}。P4b（母集団拡張）の前提である"
            " instrument_type の充填が済んでいない"
            "（未充填のままだとガード(c)は active 全体の分母へ縮退する）"
        )
