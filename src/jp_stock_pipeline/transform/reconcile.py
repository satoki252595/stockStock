"""独立ソースとの突合検証 (DESIGN.md §3-5)。

- ② に入っている値を、別ソースの実データ（既定: stooq 日足）と銘柄毎に突合する
- 乖離閾値（既定: 終値±1%）を超えた行に「要確認」フラグを付与する
- **値の自動書き換えは絶対にしない**
- より信頼できるソースで更新する場合のみソース名を更新する
  （adopt_confirmed_value。呼ぶかどうかはジョブ側の明示的な判断）

ソース非依存: J-Quants/stooq/将来の商用ベンダーなど、第2ソースが何であっても
同じ突合機構を使う（コレクターを差し替えるだけで済むようソースを抽象化する §11）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from ..contracts.stock_code import parse_stock_code, source_code_to_ticker
from ..models import DataQuality, PriceTechnicalRecord, Source

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_PCT = 1.0


@dataclass
class Discrepancy:
    """② の値と第2ソースの値の乖離。"""

    code: str
    ours: float  # ② に入っている終値
    theirs: float  # 第2ソース（突合相手）の終値
    deviation_pct: float


class ReconcileDataError(Exception):
    """② 側が正準でない銘柄コードを持っていた（突合検証が見つけるべきデータ破損）。"""


def normalize_code(raw: str | None) -> str | None:
    """第2ソースの銘柄コードを 4 文字基準へ正規化する。妥当でなければ None。

    判定と正規化は `contracts/stock_code.py` の `source_code_to_ticker` に委譲する。

    旧実装は「5 文字なら先頭4文字、それ以外は入力をそのまま返す」だったため、
    妥当性を一切見ずに突合辞書のキーを作っていた:

    - `"25935"`（伊藤園 第1種優先株式）を `"2593"`（同社 普通株）のキーにしていた。
      第2ソースに種類株の終値が混ざっていれば、普通株の終値と比べて偽の乖離を
      報告する（あるいは本物の乖離を優先株の値で塗り潰す）。
    - `str(raw)` を通していたため `None` が文字列 `"None"` というキーになっていた。
    - `"0720"` / `"A130"` / `"7203.T"` のような実在しないコードも素通りしていた。
    """
    return source_code_to_ticker(raw)


def reconcile_prices(
    other_df: pd.DataFrame,
    current_records: list[PriceTechnicalRecord],
    *,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    code_col: str = "Code",
    close_col: str = "Close",
) -> list[Discrepancy]:
    """第2ソースの終値と ② の終値を銘柄毎に突合する (§3-5)。

    threshold_pct を超える乖離のみ Discrepancy として返す。
    どちらかが欠損の銘柄は比較しない（欠損は欠損 §3-1。乖離扱いもしない）。
    """
    if other_df.empty:
        return []
    theirs_by_code: dict[str, float] = {}
    unparsable = 0
    for _, row in other_df.iterrows():
        close = row.get(close_col)
        if close is None or pd.isna(close):
            continue
        # 第2ソース（外部）のコードは 5 文字形式で来ることも 4 文字のこともある。
        # 正準形にできないものは突合対象から外す（勝手に丸めると別銘柄と
        # 突き合わせる。旧実装は "25935" を "2593" のキーにしていた）。
        their_code = normalize_code(row[code_col])
        if their_code is None:
            unparsable += 1
            continue
        theirs_by_code[their_code] = float(close)
    if unparsable:
        # 黙って捨てない。第2ソースの仕様変更や列ズレはここに現れる。
        logger.warning(
            "突合: 第2ソースの %d 行を正準コードにできず突合対象から除外した"
            "（列 %s の形式を確認すること）",
            unparsable,
            code_col,
        )

    discrepancies: list[Discrepancy] = []
    for record in current_records:
        # ② 側のコードは**既に正準4文字**である前提（①銘柄マスタが正本）。
        # ここで None が出るのは「② 自体が非正準コードを持っている」＝突合検証が
        # 見つけるべきデータ破損なので、第2ソース側と同じようにスキップして
        # しまうと検証の目的そのものを取り落とす。§3-1 の「欠損は欠損」は
        # **取得できなかった値**の話で、破損データを黙って飛ばす許可ではない。
        our_code = parse_stock_code(record.code)
        if our_code is None:
            raise ReconcileDataError(
                f"② の銘柄コードが正準形でない: {record.code!r}。"
                "①銘柄マスタとの整合を確認すること（突合は中断した）。"
            )
        theirs = theirs_by_code.get(our_code)
        if theirs is None or theirs == 0 or record.close is None:
            continue
        deviation = abs(record.close - theirs) / theirs * 100.0
        if deviation > threshold_pct:
            discrepancies.append(
                Discrepancy(
                    code=record.code,
                    ours=record.close,
                    theirs=theirs,
                    deviation_pct=deviation,
                )
            )
    return discrepancies


def apply_flags(
    records: list[PriceTechnicalRecord], discrepancies: list[Discrepancy]
) -> list[PriceTechnicalRecord]:
    """乖離銘柄の provenance.quality を「要確認」に設定する (§3-5)。

    値（close 等）は一切書き換えない。フラグ付与された record のリストを返す。
    """
    flagged_codes = {d.code for d in discrepancies}
    flagged: list[PriceTechnicalRecord] = []
    for record in records:
        if record.code in flagged_codes:
            record.provenance.quality = DataQuality.NEEDS_REVIEW
            flagged.append(record)
    return flagged


def adopt_confirmed_value(
    record: PriceTechnicalRecord, confirmed_close: float, source: Source
) -> PriceTechnicalRecord:
    """確定値（より信頼できるソースの終値）の明示的な採用 (§3-5 の更新規則)。

    自動では呼ばれない。ジョブ側が「より信頼できるソースで更新する」と
    判断した場合のみ使い、ソース名を採用元 source へ更新し品質を正常へ戻す。
    """
    record.close = confirmed_close
    record.provenance.source = source
    record.provenance.quality = DataQuality.OK
    return record
