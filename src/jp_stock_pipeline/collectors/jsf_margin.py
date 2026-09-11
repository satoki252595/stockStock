"""日本証券金融 (JSF) の貸借取引データ (docs/CF-CANONICAL-DESIGN.md ⑧'需給)。

JPX の信用残が PDF・週次・直近5週しか残らないのに対し、日証金は **CSV・毎営業日・
URL 固定**で、融資/貸株の株数と金額に加えて回転日数まで付く。実装コストが最も低く、
**過去分が一切残らない**（最新スナップショットのみ）ので、取り逃した日は永久に
埋まらない。

3ファイルとも取得元は `https://www.taisyaku.jp/data/{name}.csv`:
- `zandaka.csv`  銘柄別貸借残高（確報。毎営業日 11 時頃）。36 列
- `shina.csv`    品貸料率＝逆日歩。4 行ヘッダ。未確定値は `*****`。16 列
- `meigara.csv`  貸借取引対象銘柄の区分。2 行ヘッダ。11 列

ライセンス: **personal-only**。利用規約に「私的利用の範囲を超えて利用することは
できず…第三者の利用に供することを固く禁じます」と明文がある。公開 API・
エクスポート・公開 Worker に流さないこと。
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from ..http import FetchError, fetch

logger = logging.getLogger(__name__)

BASE_URL = "https://www.taisyaku.jp/data"
ENCODING = "cp932"

# 未確定値のマスク。0 と混同すると「逆日歩ゼロ」という誤った事実を作るので
# None のまま落とす (§3-1 推測・補完をしない)。
MASK = "*****"

_DATE_SLASH = re.compile(r"^(\d{4})/(\d{2})/(\d{2})$")
_DATE_PLAIN = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


@dataclass
class ZandakaRow:
    """貸借残高 1 行 = 銘柄 × 取引所区分。

    同一銘柄が取引所ごとに複数行になる（実測: 東証およびＰＴＳ / 名証 / 福証 / 札証）
    ため、主キーは (申込日, 銘柄コード, 取引所区分名)。
    """

    apply_date: date
    code: str
    name: str
    exchange: str
    report_type: str          # 速報 / 確報
    loan_new: int | None = None      # 融資新規株数
    loan_repaid: int | None = None   # 融資返済株数
    loan_bal: int | None = None      # 融資残高株数
    stock_new: int | None = None     # 貸株新規株数
    stock_repaid: int | None = None  # 貸株返済株数
    stock_bal: int | None = None     # 貸株残高株数
    net_bal: int | None = None       # 差引残高株数
    loan_bal_amount: int | None = None
    stock_bal_amount: int | None = None
    margin_buy_bal: int | None = None   # 制度信用・買残高株数
    margin_sell_bal: int | None = None  # 制度信用・売残高株数
    turn_days_total: float | None = None  # 総合回転日数

    @property
    def ratio(self) -> float | None:
        """信用倍率相当（融資残高 / 貸株残高）。分母 0 は None（無限大を作らない）。"""
        if not self.loan_bal or not self.stock_bal:
            return None
        return round(self.loan_bal / self.stock_bal, 4)


@dataclass
class ShinaRow:
    """品貸料率（逆日歩）1 行。貸株超過が出ている銘柄のみ掲載される。"""

    apply_date: date
    code: str
    name: str
    exchange: str
    excess_shares: int | None = None   # 貸株超過株数
    max_rate: float | None = None      # 最高料率（円）
    today_rate: float | None = None    # 当日品貸料率（円）
    prev_rate: float | None = None     # 前日品貸料率（円）
    note: str = ""                     # 備考（「満額」等。意味の解釈はしない）
    bid_rank: str = ""                 # 応札倍率ランク A〜F


@dataclass
class MeigaraRow:
    """貸借取引対象銘柄の区分。1=貸借銘柄 / 2=貸借融資銘柄 / 0=非制度信用銘柄。"""

    apply_date: date
    code: str
    name: str
    classes: dict[str, int] = field(default_factory=dict)  # 取引所名 -> 区分


def _parse_date(text: str) -> date | None:
    text = (text or "").strip()
    for pattern in (_DATE_SLASH, _DATE_PLAIN):
        m = pattern.match(text)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _int(text: str) -> int | None:
    """空欄・マスク・非数値は None。0 に潰さない (§3-1)。"""
    text = (text or "").strip().replace(",", "")
    if not text or text == MASK:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _float(text: str) -> float | None:
    text = (text or "").strip().replace(",", "")
    if not text or text == MASK:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _rows(content: bytes, *, skip: int) -> list[list[str]]:
    """CP932 の CSV を読み、先頭 skip 行（タイトル・注記・列名）を捨てる。"""
    text = content.decode(ENCODING, errors="replace")
    return list(csv.reader(io.StringIO(text)))[skip:]


def parse_zandaka(content: bytes) -> list[ZandakaRow]:
    """銘柄別貸借残高。ヘッダ1行。列は 36。"""
    out: list[ZandakaRow] = []
    for row in _rows(content, skip=1):
        if len(row) < 14 or not row[2].strip():
            continue
        apply_date = _parse_date(row[0])
        if apply_date is None:
            continue
        out.append(ZandakaRow(
            apply_date=apply_date, code=row[2].strip(), name=row[3].strip(),
            exchange=row[4].strip(), report_type=row[6].strip(),
            loan_new=_int(row[7]), loan_repaid=_int(row[8]), loan_bal=_int(row[9]),
            stock_new=_int(row[10]), stock_repaid=_int(row[11]), stock_bal=_int(row[12]),
            net_bal=_int(row[13]),
            loan_bal_amount=_int(row[16]) if len(row) > 16 else None,
            stock_bal_amount=_int(row[19]) if len(row) > 19 else None,
            margin_buy_bal=_int(row[21]) if len(row) > 21 else None,
            margin_sell_bal=_int(row[22]) if len(row) > 22 else None,
            turn_days_total=_float(row[29]) if len(row) > 29 else None,
        ))
    return out


def parse_shina(content: bytes) -> list[ShinaRow]:
    """品貸料率。タイトル・注記・抽出条件・列名の4行を捨てる。"""
    out: list[ShinaRow] = []
    for row in _rows(content, skip=4):
        if len(row) < 14 or not row[2].strip():
            continue
        apply_date = _parse_date(row[0])
        if apply_date is None:
            continue
        out.append(ShinaRow(
            apply_date=apply_date, code=row[2].strip(), name=row[3].strip(),
            exchange=row[4].strip(), excess_shares=_int(row[8]),
            max_rate=_float(row[9]), today_rate=_float(row[10]),
            prev_rate=_float(row[12]), note=row[13].strip(),
            bid_rank=row[15].strip() if len(row) > 15 else "",
        ))
    return out


def parse_meigara(content: bytes) -> list[MeigaraRow]:
    """貸借取引対象銘柄。タイトルと列名の2行を捨てる。"""
    rows = _rows(content, skip=1)
    if not rows:
        return []
    header = [c.strip() for c in rows[0]]
    out: list[MeigaraRow] = []
    for row in rows[1:]:
        if len(row) < 4 or not row[1].strip():
            continue
        apply_date = _parse_date(row[0])
        if apply_date is None:
            continue
        classes: dict[str, int] = {}
        for index in range(3, min(len(row), len(header))):
            label = header[index]
            # 空列見出し「－」は列そのものが無意味なので落とす
            if not label or label == "－":
                continue
            value = _int(row[index])
            if value is not None:
                classes[label] = value
        out.append(MeigaraRow(
            apply_date=apply_date, code=row[1].strip(), name=row[2].strip(),
            classes=classes,
        ))
    return out


def fetch_csv(name: str) -> bytes:
    """{name}.csv を取得する。中身が CSV でなければ FetchError。"""
    url = f"{BASE_URL}/{name}.csv"
    resp = fetch(url, headers={"Accept": "text/csv,*/*"})
    content = resp.content
    if not content or len(content) < 100:
        raise FetchError(f"日証金 {name}.csv が空か短すぎる: {len(content)} bytes")
    head = content[:200].decode(ENCODING, errors="replace")
    if "," not in head:
        raise FetchError(f"日証金 {name}.csv が CSV でない: {head[:80]!r}")
    return content


__all__ = [
    "BASE_URL",
    "MASK",
    "MeigaraRow",
    "ShinaRow",
    "ZandakaRow",
    "fetch_csv",
    "parse_meigara",
    "parse_shina",
    "parse_zandaka",
]
