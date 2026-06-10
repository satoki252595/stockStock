"""PDF → テキスト変換 (DESIGN.md §5.2)。

- pypdfium2 で全ページの textpage 抽出を連結する
- **OCR はしない**（真実性優先 §5.2）。画像のみ等で抽出テキストが空/ほぼ空
  （空白除去で MIN_TEXT_CHARS 文字未満）の場合は None を返し、呼び出し側は
  「変換版なし（convert_status=対象外）」として記録する
- 暗号化PDF・破損PDFは pypdfium2 の例外をそのまま送出する。呼び出し側
  （convert_artifact）が握りつぶして convert_status=FAILED を記録する
- 値不変 (§5.2 / CONTRACTS 不変条件6): 抽出されたテキストを一切修正しない
"""

from __future__ import annotations

import pypdfium2 as pdfium

# 抽出テキストの最小有意文字数（空白除去後）。これ未満は「抽出不能」扱い (§5.2)
MIN_TEXT_CHARS = 50

# ページ間の区切り（抽出結果の連結のみで、テキスト自体は変更しない）
PAGE_SEPARATOR = "\n"


def has_meaningful_text(text: str) -> bool:
    """抽出テキストが有意な量か判定する（空白除去で MIN_TEXT_CHARS 文字以上）。

    画像のみPDF等の「ほぼ空」判定ロジック (§5.2)。pdf_to_text() の内部で使うほか、
    テストで（PDFを捏造せずに）閾値ロジックを文字列レベルで検証するために公開する。
    """
    return len("".join(text.split())) >= MIN_TEXT_CHARS


def pdf_to_text(pdf_bytes: bytes) -> str | None:
    """PDF全ページのテキストを抽出して連結する (§5.2 PDF→テキスト)。

    - 戻り値 None = 抽出不能（空/ほぼ空）。変換版を作らず「対象外」と記録する
    - 暗号化・破損PDFは pypdfium2.PdfiumError 等の例外が送出される
      （呼び出し側で FAILED を記録 §5.2）
    """
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        parts: list[str] = []
        for page in pdf:
            textpage = page.get_textpage()
            try:
                parts.append(textpage.get_text_bounded())
            finally:
                textpage.close()
                page.close()
    finally:
        pdf.close()
    text = PAGE_SEPARATOR.join(parts)
    if not has_meaningful_text(text):
        return None
    return text
