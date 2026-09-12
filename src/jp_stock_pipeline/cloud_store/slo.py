"""データセットごとの鮮度 SLO（docs/TARGET-ARCHITECTURE.md §7.1）。

設計書 3 本を `鮮度|SLO|許容遅延|freshness|staleness` で検索しても**閾値が
1 つも定義されていなかった**。定義が無いので「33 日古い」が異常かどうかを
誰も判定できず、優待が 81.5 日止まっていても気づけなかった。

閾値は「実測値 + 余裕」で置いた仮値であり、業務上の根拠は無い。
運用しながら締めていく前提で、根拠は各行のコメントに残す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class FreshnessSlo:
    """1 データセットの鮮度目標。時間単位。"""

    dataset: str
    green_hours: float
    yellow_hours: float
    note: str

    def judge(self, age_hours: float | None) -> str:
        """green / yellow / red / unknown のいずれかを返す。"""
        if age_hours is None:
            return "unknown"
        if age_hours <= self.green_hours:
            return "green"
        if age_hours <= self.yellow_hours:
            return "yellow"
        return "red"


_DAY = 24.0

# 2026-09-12 の実測を踏まえた初期値。
SLOS: tuple[FreshnessSlo, ...] = (
    FreshnessSlo(
        "prices_daily", 30, 48,
        "日次。実測 2.4h。営業日翌朝までに入っていれば緑",
    ),
    FreshnessSlo(
        "tdnet_disclosures", 30, 48,
        "日次。実測 約11h",
    ),
    FreshnessSlo(
        "edinet_documents", 30, 48,
        "日次。実測 約11h。11営業日の空振りを検知できなかった対象",
    ),
    FreshnessSlo(
        "jsf_supply", 30, 48,
        "日次。実測 0.5日。日証金は最新スナップショットのみで取り逃すと埋まらない",
    ),
    FreshnessSlo(
        "core_stocks", 40 * _DAY, 45 * _DAY,
        "月次。実測 33.0日。JPX の 404 で 1 ヶ月止まっていた",
    ),
    FreshnessSlo(
        "financials", 48, 7 * _DAY,
        "提出から。D1 側は現在 0 行なので赤",
    ),
    FreshnessSlo(
        "yutai_benefits", 40 * _DAY, 50 * _DAY,
        "実測 81.5日 = 赤。再構築の方針が決まるまで赤のまま",
    ),
)

SLO_BY_DATASET: dict[str, FreshnessSlo] = {s.dataset: s for s in SLOS}


def age_hours(updated_at_epoch: int | None, *, now: datetime | None = None) -> float | None:
    """epoch 秒からの経過時間。None は unknown。"""
    if not updated_at_epoch:
        return None
    current = now or datetime.now(UTC)
    return (current.timestamp() - updated_at_epoch) / 3600.0


def judge(dataset: str, updated_at_epoch: int | None, *, now: datetime | None = None) -> str:
    """データセットの鮮度を判定する。SLO 未定義は unknown（緑にしない）。"""
    slo = SLO_BY_DATASET.get(dataset)
    if slo is None:
        return "unknown"
    return slo.judge(age_hours(updated_at_epoch, now=now))
