"""データセットごとの鮮度 SLO（docs/TARGET-ARCHITECTURE.md §7.1）。

設計書 3 本を `鮮度|SLO|許容遅延|freshness|staleness` で検索しても**閾値が
1 つも定義されていなかった**。定義が無いので「33 日古い」が異常かどうかを
誰も判定できず、優待が 81.5 日止まっていても気づけなかった。

閾値は「実測値 + 余裕」で置いた仮値であり、業務上の根拠は無い。
運用しながら締めていく前提で、根拠は各行のコメントに残す。

## 何を基準に加齢を測るか（ここが最も間違えやすい）

**取得時刻ではなく、データ基準日で測る。** 取得時刻で測ると壊れても緑になる。
実例: `jss_supply_latest` は `data_date=2026-09-10` なのに `fetched_at` は
`2026-09-11T14:58Z`（実測 21.7h）。日証金が同じスナップショットを返し続けても
`supply_daily.py` が `fetched_at = now_jst()` を**全行に塗り直す**ので、
取得時刻基準なら永遠に緑になる。SLO の note 自身が「取り逃すと埋まらない」と
書いている失敗モードがこれ。

## なぜ営業日で加齢するか

収集ジョブの cron は平日のみ（prices_daily `30 10 * * 1-5` / edinet_daily
`0 12 * * 1-5` / supply_daily `17 3 * * 1-5` / tdnet_hourly `0 0-10 * * 1-5`）。
暦時間の 30h/48h をそのまま当てると、土曜に jsf_supply が黄、日曜〜月曜が赤、
prices_daily / edinet / tdnet も日曜〜月曜が黄〜赤になる。**週 3 日鳴る通知は
見なくなる通知**で、`jobs/ops_check.py` 冒頭のコメント自身が警戒している状態。
だから日次データセットは土日を差し引いて加齢する。

## 祝日は扱っていない（＝祝日には鳴る。未解決の残件）

祝日カレンダー（`jpholiday` 等）は入れていない。依存とメンテ対象が増えるのが
理由だが、**「1 営業日分の余裕で吸収できる」とは言えない**ので誤解しないこと。
現在の閾値（日次 30h/48h）と観測時刻（23:30 JST）での実測はこうなる:

| 状態 | 加齢 | 判定 | ops_check |
|---|---|---|---|
| 当日データが入っている | 23.5h | green | 静か |
| 1 営業日欠落（= 単発の祝日と同じ） | 47.5h | **yellow** | **Issue が立つ** |
| 2 営業日欠落（= 連休・年末年始） | 71.5h | **red** | **Issue が立つ** |

`jobs/ops_check.py` は yellow も違反として扱う（exit 1）ので、**単発の祝日で
黄、年末年始・GW のような連休で赤が出る**。週末に毎週鳴る問題は消えたが、
祝日に鳴る問題は残っている（年 20〜25 日程度の見込み）。

これを消すには次のどれかを選ぶ必要があり、どれも検知力か依存を差し出すので
勝手に決めずユーザ判断に残す:

1. 日次の緑を 1 営業日ぶん緩める（実質 54h/72h）。単発祝日は緑になるが、
   **本物の 1 日欠落も緑**になり検知が 1 日遅れる。
2. 祝日カレンダーを入れる。正確だが依存とメンテ（大納会・大発会・臨時休場）が
   増える。
3. yellow を Issue にしない。本物の 1 日欠落も鳴らなくなる。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from ..models import JST

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FreshnessSlo:
    """1 データセットの鮮度目標。時間単位。

    `business_days` と `lag_days` は**明示フィールド**にしてある。データセット名から
    「日次っぽいから営業日」と暗黙に決めると、新しいデータセットを足した人が
    どちらで測られるか分からなくなる。
    """

    dataset: str
    green_hours: float
    yellow_hours: float
    note: str
    # True: 土日を差し引いて加齢する（平日 cron の日次データセット）
    # False: 暦日で加齢する（月次データセット。土日で止まっても閾値が日単位なので影響が無い）
    business_days: bool = False
    # 一次ソース自体の公表遅延（営業日）。データ基準日にこれを足した時点を
    # 「あるべき到着時刻」とみなす。0 は当日公表。
    lag_days: int = 0

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
        business_days=True,
    ),
    FreshnessSlo(
        "tdnet_disclosures", 30, 48,
        "日次。実測 約11h",
        business_days=True,
    ),
    FreshnessSlo(
        "edinet_documents", 30, 48,
        "日次。実測 約11h。11営業日の空振りを検知できなかった対象",
        business_days=True,
    ),
    FreshnessSlo(
        "jsf_supply", 30, 48,
        "日次。実測 0.5日。日証金は最新スナップショットのみで取り逃すと埋まらない。"
        "貸借残は翌営業日 11 時頃の公表なので data_date は常に T-1（lag_days=1）",
        business_days=True,
        lag_days=1,
    ),
    FreshnessSlo(
        "core_stocks", 40 * _DAY, 45 * _DAY,
        "月次。実測 33.0日。JPX の 404 で 1 ヶ月止まっていた",
    ),
    FreshnessSlo(
        "financials", 48, 7 * _DAY,
        "提出から。D1 側は現在 0 行なので赤",
        business_days=True,
    ),
    FreshnessSlo(
        "yutai_benefits", 40 * _DAY, 50 * _DAY,
        "実測 81.5日 = 赤。再構築の方針が決まるまで赤のまま",
    ),
)

SLO_BY_DATASET: dict[str, FreshnessSlo] = {s.dataset: s for s in SLOS}

# 「赤だと分かっていて、今は直せない」データセット。
#
# 観測を配線しただけでは financials（jss_financials が 0 行）と yutai_benefits
# （実測 81.96 日 / 赤閾値 50 日）が赤のまま残り、ops_check が**毎日** exit 1 して
# Issue にコメントが積まれる。それは (1) 通知を見なくなる (2) 新しい赤が埋もれる
# の二重の害があるので、既知の赤は「宣言済み」として警告に落とす。
#
# **理由の文字列を必須にしてある**。理由が無い受容は「消音」と区別できず、
# 誰も外せなくなるため。受容期限（いつまで）は入れない: いつまで受容するかは
# 投資判断の問題でユーザにしか決められないので、勝手な日付を置くと
# 「期限が来たのでまた鳴る」だけになる。代わりに**何が決まれば外せるか**を書く。
ACCEPTED_RED: dict[str, str] = {
    "financials": (
        "jss_financials は 0 行（writer 未実装）。EDINET/TDnet の XBRL から"
        "財務サマリを作る writer をどちらの経路で実装するか（EDINET 正本か"
        "短信 inline XBRL か）が決まれば外せる"
    ),
    "yutai_benefits": (
        "実測 81.96 日。優待の一次ソース（みんかぶ）は規約上再取得できず、"
        "kabulab-cf の LLM 推定値が再取得不能な資産として残っている。"
        "①代替の一次ソースを決める か ②優待を「更新しないデータセット」として"
        "SLO から外す のどちらを採るかが決まれば外せる"
    ),
}


def age_hours(updated_at_epoch: int | None, *, now: datetime | None = None) -> float | None:
    """epoch 秒からの経過時間（暦時間）。None は unknown。

    互換のために残している素朴な版。鮮度判定には `judge_observation` を使うこと
    （こちらは営業日もデータ基準日も考慮しない）。
    """
    if not updated_at_epoch:
        return None
    current = now or datetime.now(UTC)
    return (current.timestamp() - updated_at_epoch) / 3600.0


def judge(dataset: str, updated_at_epoch: int | None, *, now: datetime | None = None) -> str:
    """取得時刻だけで判定する素朴な版（暦時間）。

    新規の呼び出しは `judge_observation` を使うこと。こちらは取得時刻を信じるので
    「壊れた writer が now を塗り直す」ケースで偽の緑を出す。
    """
    slo = SLO_BY_DATASET.get(dataset)
    if slo is None:
        return "unknown"
    return slo.judge(age_hours(updated_at_epoch, now=now))


# --- 加齢の計算 -----------------------------------------------------------


def _weekend_days_after(start: date, end: date) -> int:
    """(start, end] に含まれる土日の日数。

    start 自身は数えない（start は「あるべき到着時刻」の当日で、その日は
    加齢の起点だから）。差し引きは 1 日 24h 単位なので、起点が日中にあると
    過剰に引く可能性があるが、過剰に引く方向は必ず「より緑」＝**誤警報を
    増やさない**側なので許容する（下で 0 にクランプする）。
    """
    if end <= start:
        return 0
    count = 0
    day = start + timedelta(days=1)
    while day <= end:
        if day.weekday() >= 5:  # 5=土 6=日
            count += 1
        day += timedelta(days=1)
    return count


def _add_business_days(start: date, days: int) -> date:
    """営業日（平日）で days 日進める。days=0 はそのまま返す。"""
    day = start
    remaining = days
    while remaining > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            remaining -= 1
    return day


def elapsed_hours(base: datetime, now: datetime, *, business_days: bool) -> float:
    """base から now までの経過時間。business_days なら土日を差し引く。

    JST の暦日で土日を数える（データ基準日が JST の営業日文字列なので、
    UTC で数えると境界が 9 時間ずれる）。
    """
    hours = (now - base).total_seconds() / 3600.0
    if business_days:
        hours -= _DAY * _weekend_days_after(
            base.astimezone(JST).date(), now.astimezone(JST).date()
        )
    return max(0.0, hours)


def _parse_data_date(value: date | str | None) -> date | None:
    """データ基準日を date にする。読めない値は None（取得時刻へフォールバック）。"""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        logger.warning("データ基準日として読めない値: %r", value)
        return None


def _base_datetime(
    slo: FreshnessSlo, latest_data_date: date | str | None, source_epoch: int | None
) -> datetime | None:
    """加齢の起点（「あるべき到着時刻」）を決める。

    データ基準日があればそれ + lag_days 営業日の 00:00 JST。無ければ取得時刻。
    どちらも無ければ None（unknown）。
    """
    data_date = _parse_data_date(latest_data_date)
    if data_date is not None:
        due = _add_business_days(data_date, slo.lag_days)
        return datetime(due.year, due.month, due.day, tzinfo=JST)
    if source_epoch:
        return datetime.fromtimestamp(float(source_epoch), tz=UTC)
    return None


def observation_age_hours(
    dataset: str,
    *,
    latest_data_date: date | str | None,
    source_epoch: int | None,
    now: datetime | None = None,
) -> float | None:
    """判定に使う加齢時間。人向けメッセージ用に切り出してある。"""
    slo = SLO_BY_DATASET.get(dataset)
    if slo is None:
        return None
    base = _base_datetime(slo, latest_data_date, source_epoch)
    if base is None:
        return None
    return elapsed_hours(base, now or datetime.now(UTC), business_days=slo.business_days)


def judge_observation(
    dataset: str,
    *,
    latest_data_date: date | str | None,
    source_epoch: int | None,
    row_count: int | None,
    now: datetime | None = None,
) -> str:
    """観測結果から鮮度を判定する。green / yellow / red / unknown。

    優先順（この順序が仕様。入れ替えると偽の緑・偽の unknown が出る）:
      (a) row_count == 0 → **red**。測った結果ゼロ件は「分からない」ではなく異常。
      (b) row_count is None → unknown。そもそも測れていない。
      (c) データ基準日があればその加齢で判定（取得時刻は信じない）。
      (d) 基準日の列が無い表だけ取得時刻の加齢で判定。
    """
    slo = SLO_BY_DATASET.get(dataset)
    if slo is None:
        return "unknown"
    if row_count == 0:
        return "red"
    if row_count is None:
        return "unknown"
    return slo.judge(
        observation_age_hours(
            dataset,
            latest_data_date=latest_data_date,
            source_epoch=source_epoch,
            now=now,
        )
    )
