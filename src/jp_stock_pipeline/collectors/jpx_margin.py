"""JPX「銘柄別信用取引（週末）残高」PDF のコレクター (移行 P2)。

kabulab-cf の `services/vwap-analysis/lib/margin.ts` を Python へ移植したもの。
R2 `vwap-data/margin/{date}.json` の writer を stockStock へ移管するため、
**出力が1バイトも変わらないこと**を最優先にしている（実PDF 5週分で TS 実装との
完全一致を検証済み。tests/test_jpx_margin.py）。

## 2026-09-28 の様式変更

JPX は 2026-09-28 から週次→毎営業日16:00へ変更し、様式も変える
（金額行の追加・上場比の追加・銘柄コード順化）。旧様式のパースは
`parse_margin_text` が、新様式は公開後に実データを見てから追加する。
どちらの様式かは `detect_layout()` がヘッダ文字列で判定し、**判定できない
テキストは推測せず LayoutUnknown を返す**（§3-1 推定しない）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# kabulab-cf の実装と同一の UA。JPX は UA 無しだと 403 を返す。
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
BASE_URL = "https://www.jpx.co.jp"
INDEX_URL = f"{BASE_URL}/markets/statistics-equities/margin/05.html"

# 一覧ページの PDF リンク（syumatsu{YYYYMMDD}{連番2桁}.pdf）
_PDF_LINK_RE = re.compile(
    r"/markets/statistics-equities/margin/[^\"']*?syumatsu(\d+)\.pdf"
)

# 「2026/9/4 申込み現在」から基準日を取る。
_WEEK_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})\s*申込")

# 数値トークン（▲ は負号。全角スペースを挟む場合がある）
_NUM = r"(?:▲\s*)?[\d,]+"

# 明細行: 5桁コード + ISIN + 売残/前週比/買残/前週比。
# コードの4文字目は英字を取りうる（例: 130A0 → 130A）。
_ROW_RE = re.compile(
    rf"(\d{{3}}[0-9A-Z]\d)\s+(JP\w{{10}})\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})"
)


class Layout(StrEnum):
    """PDF の様式。"""

    WEEKLY = "weekly"      # 〜2026-09-25 の週次様式
    DAILY = "daily"        # 2026-09-28〜 の日次様式（未実装）
    UNKNOWN = "unknown"    # 判定不能（推測せず失敗として扱う）


@dataclass(frozen=True)
class MarginRow:
    """`margin/{date}.json` の rows[] 1件。**このキー構成は契約なので変えない。**"""

    code: str
    sell: int
    sell_chg: int
    buy: int
    buy_chg: int

    def to_dict(self) -> dict:
        # キーの順序も kabulab-cf の出力と揃える（差分比較を容易にするため）
        return {
            "code": self.code,
            "sell": self.sell,
            "sell_chg": self.sell_chg,
            "buy": self.buy,
            "buy_chg": self.buy_chg,
        }


@dataclass(frozen=True)
class MarginData:
    week: str
    rows: list[MarginRow]

    def to_dict(self) -> dict:
        return {"week": self.week, "rows": [r.to_dict() for r in self.rows]}


def to_int(text: str) -> int:
    """「▲ 1,600」→ -1600。解釈できない値は 0（kabulab-cf の `|| 0` と同じ）。

    0 へ倒すのは推測ではなく既存実装との**完全互換のため**。ここを None に
    変えると R2 の既存契約（数値であること）と読み手の実装を壊す。
    """
    cleaned = (
        text.replace(",", "").replace("▲", "-").replace(" ", "").replace("　", "")
    )
    try:
        return int(cleaned)
    except ValueError:
        return 0


def detect_layout(text: str) -> Layout:
    """PDF テキストから様式を判定する。判定できなければ UNKNOWN。

    週次様式は列見出しに「前週比 / Weekly change」を持つ。日次様式は
    「前日比」および「上場比」を持つ（2026-09-28〜。JPX 通知に基づく想定で、
    実データ公開後に実物で確認する必要がある）。
    """
    has_weekly = "前週比" in text or "Weekly change" in text
    has_daily = "前日比" in text
    if has_daily and not has_weekly:
        return Layout.DAILY
    if has_weekly and not has_daily:
        return Layout.WEEKLY
    return Layout.UNKNOWN


def parse_margin_text(text: str) -> MarginData:
    """週次様式のテキストを解析する。

    kabulab-cf の `parseMarginText` と**同一の正規表現・同一の数値変換**を使う。
    実PDF 5週分で出力の完全一致を検証済み。
    """
    m = _WEEK_RE.search(text)
    week = (
        f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""
    )
    rows = [
        MarginRow(
            code=mm.group(1)[:4],
            sell=to_int(mm.group(3)),
            sell_chg=to_int(mm.group(4)),
            buy=to_int(mm.group(5)),
            buy_chg=to_int(mm.group(6)),
        )
        for mm in _ROW_RE.finditer(text)
    ]
    return MarginData(week=week, rows=rows)


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """PDF 全ページのテキストを結合する。"""
    import pypdfium2 as pdfium  # noqa: PLC0415 - 重い依存を import 時に引かない

    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        return "\n".join(page.get_textpage().get_text_range() for page in pdf)
    finally:
        pdf.close()


def list_pdf_urls(index_html: str) -> list[str]:
    """一覧ページから PDF の絶対 URL を古い順に返す。"""
    found = sorted(
        {(m.group(1), m.group(0)) for m in _PDF_LINK_RE.finditer(index_html)}
    )
    return [BASE_URL + path for _stamp, path in found]


def latest_pdf_url(index_html: str) -> str:
    """一覧ページの最新 PDF URL。無ければ ValueError。"""
    urls = list_pdf_urls(index_html)
    if not urls:
        raise ValueError("JPX 信用残 PDF のリンクが一覧ページに無い")
    return urls[-1]
