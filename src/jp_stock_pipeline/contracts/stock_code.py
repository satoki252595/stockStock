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

# 全角英数字 → 半角のオフセットと、変換対象の 3 レンジ。
# TypeScript 側の正規表現リテラル `/[０-９Ａ-Ｚａ-ｚ]/g` と**同じ文字集合**にする。
# 以前は U+FF10-U+FF5A を 1 本の連続範囲で変換していたため、レンジの隙間にある
# 全角記号 (U+FF1A-U+FF20 の `：；＜＝＞？＠` 等) まで半角化し、同じ入力で
# normalize の答えが TS と割れていた (実測: "７２０３：" → Python "7203:" /
# TS "7203："). 妥当性判定は両者 None なので今日は無害だが、normalize の出力は
# kabulab-cf 側で core_stocks に書く値そのもの。
_FULLWIDTH_OFFSET = 0xFEE0
_FULLWIDTH_ALNUM_RE = re.compile(r"[\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]")

# JS の `String.prototype.trim()` が落とす文字集合 (WhiteSpace ∪ LineTerminator)。
# Python の `str.strip()` の既定はこれと一致しない: U+FEFF (BOM) を落とさず、
# 逆に U+001C-U+001F / U+0085 を落とす。UTF-8-sig の CSV 先頭セルには BOM が
# 実際に付くので、既定のままでは同じ入力で TS が "7203" を、Python が None を
# 返す (= 片方だけ銘柄を取りこぼす) 割れになる。明示集合で揃える。
_JS_TRIM_CHARS = (
    "\t\n\v\f\r \u00a0\u1680"
    + "".join(chr(c) for c in range(0x2000, 0x200B))  # U+2000-U+200A (Zs)
    + "\u2028\u2029\u202f\u205f\u3000\ufeff"
)

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
    text = str(code).strip(_JS_TRIM_CHARS)
    half = _FULLWIDTH_ALNUM_RE.sub(
        lambda m: chr(ord(m.group(0)) - _FULLWIDTH_OFFSET), text
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
    根拠に厳格側へ寄せた未検証の変更である。末尾非 0 (例 合成コード `1202` の
    ``"12024"``) は None になり、呼び出し側で母集団外として落ちる。
    取り違えより取りこぼしを選ぶ。
    """
    normalized = normalize_stock_code(code)
    if len(normalized) == 4:
        return normalized if STOCK_CODE_RE.match(normalized) else None
    if len(normalized) == 5 and normalized.endswith(SOURCE_CHECK_CHAR):
        head = normalized[:4]
        return head if STOCK_CODE_RE.match(head) else None
    return None


# 信用残 PDF で「別の証券」として 5 文字のまま残す検査文字 (数字の 1-9)。
_CLASS_SHARE_CHECK_CHARS = frozenset("123456789")


def margin_code_to_key(code: str | None) -> str | None:
    """JPX 信用残 PDF の 5 文字コードを R2 `margin/{date}.json` の `rows[].code` へ。

    - 末尾 "0" (と 4 文字) は `source_code_to_ticker` と同じ 4 文字ティッカー。
    - 「4 文字の正準形 + 数字 1-9」の 5 文字は **5 文字のまま** 返す (種類株)。
    - それ以外は None。

    ## なぜ `source_code_to_ticker` をそのまま使わないか
    実 PDF (2026-08-28 / 09-04 申込み現在、各 4,229 / 4,227 明細) の実測:

    - 検査文字は "0" が 4,222 / 4,220 行、非 "0" は **両週とも 7 行だけ**
      ("5" が 6 行・"6" が 1 行)。ETF・ETN・REIT・インフラファンド・JDR は
      **全件 "0"**。「ETF/REIT は非 0 の見込み」という懸念は外れていた
      (TDnet の ``"12024"`` のような形は信用残 PDF には無い)。
    - 非 "0" の 7 行はすべて種類株 (伊藤園第１種優先株式 25935、
      ゼンショー・日本航空・ANA・インフロニア・ソフトバンク×2 の社債型種類株式)
      で、**全件が同社普通株と先頭 4 文字を共有**する。旧実装 (先頭 4 文字切り
      出し) は 6 つの 4 文字コードに 13 行を潰していた (取り違え)。

    `source_code_to_ticker` を当てると取り違えは消えるが、この 7 行 (実在する
    別の証券の残高) を**落とす**。信用残は「その証券の行」を 1 件も捨てずに
    写す writer なので、落とさず 5 文字のまま別キーにする。5 文字キーは
    4 文字ティッカーと決して衝突しないため取り違えも起きない。

    ## 採らなかった案
    - ISIN を rows に足す: 公開 API が行を丸ごとスプレッドするため公開面が
      広がり、rows[] のキー固定契約 (kabulab-cf `margin.test.ts` が固定) を破る。
    - 非 "0" 行を捨てる: 上記のとおり実在証券のデータ欠損になる。

    ## 範囲
    TDnet/EDINET の取込には使わない (そちらは 4 文字の銘柄母集団へ突合するので
    `source_code_to_ticker` の「取りこぼし側」が正しい)。
    """
    ticker = source_code_to_ticker(code)
    if ticker is not None:
        return ticker
    normalized = normalize_stock_code(code)
    if (
        len(normalized) == 5
        and STOCK_CODE_RE.match(normalized[:4])
        and normalized[4] in _CLASS_SHARE_CHECK_CHARS
    ):
        return normalized
    return None
