"""銘柄コード (証券コード) 正規化の正準実装。

## なぜ 1 箇所に集めるか
同じ「銘柄コードの正規化」が Python 側に 4 実装・TypeScript 側に 3 実装あり、
同一入力に対する答えが割れていた。実測 (2026-09-12) で 13 入力のうち 7 入力で
不一致。割れの本質は **3 つの別の操作を 1 つの関数で兼ねようとしたこと**:

1. `normalize_stock_code` … 表現揺れの吸収のみ (trim / 全角→半角 / 大文字化)
2. `parse_stock_code`     … 4 文字の正準形か判定する
3. `source_code_to_ticker` … TDnet/EDINET の 5 文字形式 → 4 文字ティッカー

2 と 3 を混ぜると「5 文字なら先頭 4 文字」という規則になり、別の証券を既存銘柄へ
取り違える (下記 `source_code_to_ticker` の docstring を見ること)。

## 正準形の根拠
- JPX 証券コード協議会: コードは **4 文字**。2024 年 1 月以降は英文字入りの付番が
  始まり、英字は 2 桁目と 4 桁目のいずれか (または両方) を取りうる。使用する英字は
  大文字 19 文字 (B/E/I/O/Q/V/Z を除く)。4 桁目のみ英字のコードを `130A` から
  使い切った後に 2 桁目のみ英字 (`1A00` から) へ移る。
- 本番 `core_stocks` の実測 (2026-09-12): 全 3,818 行のうち 4 文字が 3,812 行、
  5 文字が 6 行 (すべて `is_active=0` の種類株)。4 文字行のうち英字を含むのは
  174 行で、**全件が 4 桁目のみ** (1/2/3 桁目の英字は 0 件)。小文字・空白・
  先頭 0 はいずれも 0 件。

## 既知のギャップ
`STOCK_CODE_RE` は 4 桁目のみ英字を許し、除外 7 文字も絞っていない。どちらも
意図的な現状追認 (今日の実データを 1 件も落とさず 1 件も余計に通さない)。JPX が
`1A00` 台の付番を始めたらこのパターンと共有テストベクタを同時に更新する。

## 言語をまたぐ正
同じ契約を kabulab-cf の `src/shared/jpx/stock-code.ts` が持つ。期待値は
`tests/fixtures/contracts/stock-code-vectors.json` に置き、両リポジトリのテストが
同一バイト列のファイルを読む (CI の cross-repo-contract ジョブで diff)。
"""

from __future__ import annotations

import re

# 正準パターン: 数字 3 桁 + (数字 or 英大文字) 1 桁。
# `\d` ではなく `[0-9]` で書くのは、TypeScript 側の正規表現リテラルの `.source`
# と 1 文字ずつ一致させ、共有テストベクタの `canonical_regex` でパターン自体を
# 両言語で固定するため (`\d` は Python では Unicode 数字も拾うため意味も違う)。
STOCK_CODE_RE = re.compile(r"^[0-9]{3}[0-9A-Z]$")

# 全角英数字 (U+FF10-FF19 / U+FF21-FF3A / U+FF41-FF5A) → 半角のオフセット。
_FULLWIDTH_OFFSET = 0xFEE0
_FULLWIDTH_START = "０"
_FULLWIDTH_END = "ｚ"

# 取込ソース (TDnet company_code / EDINET secCode) の 5 文字形式で受理する検査文字。
SOURCE_CHECK_CHAR = "0"


def normalize_stock_code(code: str | None) -> str:
    """表記揺れを吸収する。妥当性は判定しない。

    trim → 全角英数字を半角へ → 英字を大文字化。これは「フォールバック」ではなく
    「同じ意味の値の表現揺れを揃える」処理 (§3-1 の明示的な例外)。値の意味は
    変えず表現だけ揃える。

    欠損 (None) は空文字列を返す。`str(None)` が文字列 `"None"` になる罠を
    踏まないこと (`transform/reconcile.py` の旧実装が実際に踏んでおり、
    突合辞書に `"None"` というキーを作っていた)。
    """
    if code is None:
        return ""
    text = str(code).strip()
    half = "".join(
        chr(ord(ch) - _FULLWIDTH_OFFSET)
        if _FULLWIDTH_START <= ch <= _FULLWIDTH_END
        else ch
        for ch in text
    )
    return half.upper()


def is_valid_stock_code(code: str | None) -> bool:
    """正規化後に 4 文字の正準パターンへ一致するか。5 文字の種類株はここで落ちる。"""
    return bool(STOCK_CODE_RE.match(normalize_stock_code(code)))


def parse_stock_code(code: str | None) -> str | None:
    """正規化して妥当なら正準形を返し、妥当でなければ None。

    欠損・不正を None で表現し、呼び出し側に判断を委ねる (§3-1)。
    黙って別の値へ差し替えてはならない。
    """
    normalized = normalize_stock_code(code)
    return normalized if STOCK_CODE_RE.match(normalized) else None


def source_code_to_ticker(code: str | None) -> str | None:
    """取込ソースの 5 文字コードを 4 文字ティッカー (正準形) へ変換する。

    TDnet の `company_code` と EDINET の `secCode` は「4 文字ティッカー + 検査文字
    1 文字」の 5 文字で来る (例 ``"72030"`` → ``"7203"``、``"130A0"`` → ``"130A"``)。
    4 文字でそのまま来る場合も受理する。

    ## なぜ末尾を "0" に限定するか (先頭 4 文字切り出しでは駄目な理由)
    「5 文字なら先頭 4 文字」で切ると **別の証券を既存銘柄に取り違える**。

    - ``"25935"`` (伊藤園第1種優先株式) → ``"2593"`` (伊藤園 普通株)。本番
      `core_stocks` に両方が実在するため、優先株の開示を普通株へ付け替えていた。
    - ``"07203"`` → ``"0720"``。本番に先頭 0 のコードは 1 件も無く、実在しない
      銘柄を捏造していた。

    本番 `ir_disclosures` の `company_code` 37,641 行は全件が 5 文字かつ末尾 "0"
    なので、**TDnet についてはこの限定で取りこぼしは出ない** (実測)。

    ## EDINET については未検証
    生の `secCode` を保存している表が無く分布を取れていないため、EDINET で末尾が
    "0" に限られるという実測根拠は無い。仕様上の同形性 (4 文字 + 検査文字) を
    根拠に厳格側へ寄せた未検証の変更である。末尾非 0 (例 ETF `1671` の
    ``"16714"``) は None になり、呼び出し側で母集団外として落ちる。
    取り違えより取りこぼしを選ぶ。
    """
    normalized = normalize_stock_code(code)
    if len(normalized) == 4:
        return normalized if STOCK_CODE_RE.match(normalized) else None
    if len(normalized) == 5 and normalized.endswith(SOURCE_CHECK_CHAR):
        head = normalized[:4]
        return head if STOCK_CODE_RE.match(head) else None
    return None
