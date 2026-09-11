"""①銘柄マスタの母集団ガード（移行 P4a）。

kabulab-cf の `src/cron/universe.ts` の `assertUniverseCoverage`（:82-120、定数は
:55/:57/:59/:61）の 1:1 移植。writer を stockStock へ移す前に、**同じ条件で同じ
ように止まる**ことを先に固定しておくためのモジュール。

設計書 `docs/CF-CANONICAL-DESIGN.md` は「`universe.ts:155-201` の3段ガード」と
書いているが、実コードは次の2点で異なる（2026-09-12 に実測して訂正済み）。

- 実体は `scripts/sync/universe.ts`（29行の薄い CLI）ではなく `src/cron/universe.ts`
- 「3段」ではなく **4条件**。設計書 §12 の (a)-(d) の列挙のほうが実コードと一致する

**比較の向きを移植元と厳密に合わせること。** (a)(b)(c) は「未満で失敗」、(d) は
「超過で失敗」。境界値ちょうどは通る。現行 active 3,715 なら (d) は 74 件まで通り
75 件で発火する。

**(c)(d) は `existing_active_count == 0` のとき丸ごとスキップされる。** これは
0 除算避けではなく初回 seed を通すための意図的な穴で、移植元の挙動をそのまま保つ。
SELECT の失敗を「テーブルが空」と取り違えてガードを無効化しないよう、
`assert_population_sane()` を別に用意している。
"""

from __future__ import annotations

import re

from .guards import GuardError

# --- 移植元の定数（universe.ts の行番号を併記。変更時は両方を見ること）---------
MIN_JPX_ROWS = 4_000  # universe.ts:55  MIN_JPX_ROWS
MIN_EQUITY_ROWS = 3_000  # universe.ts:57  MIN_EQUITY_ROWS
MIN_EXISTING_COVERAGE = 0.98  # universe.ts:59  MIN_EXISTING_COVERAGE
MAX_DEACTIVATION_RATIO = 0.02  # universe.ts:61  MAX_DEACTIVATION_RATIO

# JPX の4文字コード契約（英字入り新方式コード "130A" を含む）。
# kabulab-cf の src/shared/jpx/stock-code.ts:31 と同じ。
STOCK_CODE_RE = re.compile(r"^\d{3}[0-9A-Z]$")


def is_valid_stock_code(code: str | None) -> bool:
    """4文字コード契約に合格するか。5桁の種類株はここで落ちる。"""
    return bool(code) and bool(STOCK_CODE_RE.match(str(code).strip().upper()))


def assert_universe_coverage(
    raw_count: int,
    equity_count: int,
    existing_active_count: int,
    pending_deactivation_count: int,
) -> None:
    """母集団の4条件。1つでも破れたら書込ゼロで止める（universe.ts:82-120）。

    :param raw_count: JPX data_j の**全行数**。ETF/REIT/PRO/外国株を含む
        （内国株に絞る前の値）。universe.ts:159 の第1引数。
    :param equity_count: `isListedEquity` 相当を通った行数。「内国株式」かつ
        プライム|スタンダード|グロース かつ4文字コード契約、の3条件すべて。
    :param existing_active_count: `core_stocks` の **is_active=1 の行数のみ**。
        total ではない（取り違えると (c)(d) の分母が狂う）。
    :param pending_deactivation_count: これから is_active=0 にする件数。
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
    # (c) universe.ts:100-109 — existing==0 は移植元どおりスキップする
    if (
        existing_active_count > 0
        and equity_count / existing_active_count < MIN_EXISTING_COVERAGE
    ):
        raise GuardError(
            f"ガード(c): JPX 対象株 {equity_count} 件が既存 active "
            f"{existing_active_count} 件の {MIN_EXISTING_COVERAGE:.0%} 未満"
        )
    # (d) universe.ts:110-119 — 同上
    if (
        existing_active_count > 0
        and pending_deactivation_count / existing_active_count > MAX_DEACTIVATION_RATIO
    ):
        raise GuardError(
            f"ガード(d): 対象外化候補 {pending_deactivation_count} 件が既存 active "
            f"{existing_active_count} 件の {MAX_DEACTIVATION_RATIO:.0%} 超"
        )


def should_deactivate_universe_code(code: str, raw_codes: frozenset[str]) -> bool:
    """対象外化の判定（universe.ts:123-128）。

    `raw_codes` は `isListedEquity` で絞る**前**の data_j 全行のコード集合。
    ここを内国株だけに絞ると ETF や REIT が毎回対象外化されて (d) が発火する。
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
