"""xls/xlsx → CSV 変換 (DESIGN.md §5.2)。

値不変 (§5.2 / CONTRACTS 不変条件6):
- dtype=str でセル値を文字列のまま保持する（数値推定・丸め・補完はしない）
- ヘッダ加工なし: 先頭行をヘッダのまま使う（header=None にはしない）
- 欠損セルは欠損のまま（CSV では空欄になる §3-1）
- 行う処理は「Excelバイナリ → UTF-8 CSV」の形式変換のみ
"""

from __future__ import annotations

import io

import pandas as pd


def xls_to_csv(xls_bytes: bytes, filename: str) -> dict[str, bytes]:
    """Excel ファイルの全シートを UTF-8 CSV に変換する。

    Args:
        xls_bytes: 原本の無加工バイト列
        filename: 原本ファイル名（拡張子でエンジンを選ぶ: .xls→xlrd / .xlsx→openpyxl）

    Returns:
        {シート名: UTF-8 CSV バイト列}

    Raises:
        ValueError: 未対応の拡張子
        その他: 破損ファイル等は pandas/xlrd/openpyxl の例外をそのまま送出
        （呼び出し側 convert_artifact が FAILED を記録する §5.2）
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext == "xls":
        engine = "xlrd"
    elif ext in ("xlsx", "xlsm"):
        engine = "openpyxl"
    else:
        raise ValueError(f"未対応のExcel拡張子: {filename!r} (.xls/.xlsx/.xlsm のみ)")

    # sheet_name=None で全シート取得。dtype=str で値の文字列をそのまま保持
    sheets: dict[str, pd.DataFrame] = pd.read_excel(
        io.BytesIO(xls_bytes), sheet_name=None, dtype=str, engine=engine
    )
    result: dict[str, bytes] = {}
    for sheet_name, df in sheets.items():
        # 先頭行ヘッダのまま・index なし・UTF-8 (§5.2 文字コード正規化のみ)
        result[sheet_name] = df.to_csv(index=False).encode("utf-8")
    return result
