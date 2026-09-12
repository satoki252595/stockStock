"""G-fin-1: ③断面テーブルの writer 交代（P5）の切替判定。

設計書 `docs/CF-CANONICAL-DESIGN.md` の P5 節にあった旧定義は

> **G-fin-1（値一致）**: 数値列の相対誤差 ≤ 1e-6、NULL は NULL と一致。

だった。**この定義は原理的に達成できない。** 旧 writer（kabulab-cf の
`daily.ts`）と新 writer（stockStock）は別時刻に Yahoo を叩く。Yahoo は TTM
（直近12ヶ月）を改訂するので、eps / bps / roe は取得時刻が違えば値そのものが
変わる。price は日中に動く。「基準日が一致する行だけを比較する」という旧定義の
但し書きは**日付のずれしか吸収しない**ので、同じ基準日の行でも値は一致しない。

## 実測（2026-09-12。旧 writer の書いた行 vs 新 writer の書いた行）

| 項目 | 比較可能 | 一致（最厳） | 一致（最緩） | worst 相対差 |
|---|---:|---:|---:|---|
| `per` | 3,317 | **3,317** | 3,317 | 1.313e-07 |
| `pbr` | 3,736 | **3,736** | 3,736 | 1.368e-07 |
| `eps` | 1,611 | 76 | 1,465 | — |
| `bps` | 1,612 | 648 | 949 | — |
| `roe` | 1,517 | 744 | 806 | — |
| `price` | 1,616 | 23 | 314 | — |

（eps の一致件数は閾値を緩めるにつれ 76 → 430 → 1,024 → 1,376 → 1,465。
閾値の刻みは実測ログ側にあるので、ここでは**最厳 76 件・最緩 1,465 件**だけを
根拠として使う。bps 648→949 / roe 744→806 / price 23→314 も同じ読み方。）

読み取れること:

1. **`per` / `pbr` は 1e-6 を余裕で満たす**（worst 1.4e-07）。この 2 つだけは
   旧定義のままで通る
2. **`eps` / `bps` / `roe` / `price` は満たさない。** 最も緩い観測閾値でも eps は
   1,611 中 1,465（91%）、price は 1,616 中 314（19%）しか一致しない
3. **比較可能な行数が項目ごとに 1,517〜3,736 と 2 倍以上違う。** 旧定義は
   「基準日が一致する行だけを比較し、一致しない行は比較不能として別カウント」と
   **行単位**で比較可能性を切っているが、実測は**項目単位**で母数が違う。
   行単位の「比較不能」カウントでは、ある項目だけ 1,600 行しか比較できていない
   ことが見えない

つまり誤りは 1e-6 という数字ではなく、**全項目に同じ 1 個の閾値を課したこと**と
**比較可能性を行単位で切ったこと**の 2 点である。

### なぜ per/pbr だけ一致するのか（仮説・未検証）

`per` / `pbr` は提供元が**丸めて配信している公表値**を両 writer がそのまま写して
いるだけで、更新頻度も引け値ベースと見られる。一方 `price` は日中に動く値、
`eps` / `bps` / `roe` は TTM 改訂を受ける値。**これは実装を読んで確かめていない
仮説**であり、P5 の最初のタスク（実レスポンスでのキー存在確認）で
`trailingPE` 系のキーが公表値かどうかを確定させること。仮説が崩れたら
`per` / `pbr` の厳しい閾値も維持できない。

## 再定義

値の一致を全項目に課すのをやめ、**項目ごとに「何を保証できるか」を分ける**。

- **G-fin-1a 公表値の一致**（`per` / `pbr`）: 相対誤差 ≤ 1e-6。実測 worst
  1.4e-07 なので **7 倍の余裕**がある。ここだけは旧定義を据え置く
- **G-fin-1b 導出整合**（同一取得バッチ内）: `per = price / eps` と
  `pbr = price / bps` が**同じバッチの値同士で**成り立つこと。writer 間の一致では
  なく 1 スナップショット内の自己整合なので、TTM 改訂に影響されない。
  `roe = eps / bps` は **`roe` の単位（% か比か）が未確認なので入れていない**
  （`DERIVED_IDENTITIES` のコメント参照）
- **G-fin-1c 桁・符号・単位の妥当性**: `eps < 0` なのに `per > 0`、`bps <= 0`
  なのに `pbr > 0` のような矛盾が無いこと。符号の反転は TTM 改訂では起きない
  （起きたら会計基準か連結/単体の取り違え）
- **G-fin-1d 欠損パターンの一致**: 「新 writer だけ NULL」の件数が旧 writer の
  0.5% 以内（カバレッジは G-fin-2 が見るので、ここは項目単位の欠損だけ）

### 閾値を置かなかった項目について

`eps` / `bps` / `roe` / `price` の**相対差そのものには閾値を置かない**。
分布（中央値・p95・最大）を `FieldVerdict` に載せて**観測だけする**。

いま数字を決めないのは、決められる根拠が無いからである。上の実測は 1 回ぶんの
スナップショットで、閾値の刻みも粗い。ここで「eps は 5% まで」と書けば、それは
**1e-6 を置いたときと同じ**「根拠の無い 1 個の数字」になる。設計書の G-fin-1 は
もともと「3 回連続」で判定する取り決めなので、**3 回ぶんの分布が出てから
`rel_tol` を埋める**。それまでは分布を記録し、a/b/c/d で落とす。

この判断のせいで `eps` の**緩やかな系統ずれ**（例: 全銘柄が一律 3% ずれる）は
G-fin-1 を通ってしまう。その穴は承知の上で、b（導出整合）が桁・単位の誤りを、
c が符号の誤りを、G-fin-2 がカバレッジを見ている。**「通った」ことを「一致した」
と読まないよう、レポートに `観測のみ` を明示する。**

本モジュールは純粋関数だけで、D1 も R2 も読まない（呼び出しは P5 の切替ジョブ側）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import median

# --- 項目ごとの規則 ---------------------------------------------------------

#: G-fin-1a の相対誤差。実測 worst が per 1.313e-07 / pbr 1.368e-07 なので
#: 1e-6 は 7 倍の余裕がある。旧定義でこの 2 項目だけは通っていた。
PUBLISHED_REL_TOL = 1e-6

#: G-fin-1b の導出整合の許容相対差。**未実測の見積り値**。
#:
#: 提供元は per/pbr を小数 2 桁で配信し eps/bps も丸められているため、丸めの
#: 合成で 1e-3 程度の残差が出る見込み（未検証）。1% はその 10 倍の余裕で、
#: この幅の目的は精度の検証ではなく **×100 / ÷1000 の単位ミスを確実に捕まえる**
#: こと。精度としての閾値は 3 回ぶんの分布が出てから詰める。
#:
#: **未解決のリスク（2026-09-12 反証レビュー）。** この幅が成り立つかは
#: **`eps` / `bps` の配信桁数に完全に依存する**が、それは未確認である
#: （`per`/`pbr` が公表値かどうかも仮説のまま）。`eps` の丸め誤差は
#: `0.5 * 10**-桁数 / |eps|` なので、1% を超える `eps` の境界は
#:
#:   小数2桁 → |eps| < 0.5 円（数行）／小数1桁 → |eps| < 5 円（十数行）／
#:   整数 → |eps| < 50 円（**数百行**）
#:
#: と桁数ひとつで 2 桁変わる。`evaluate_parity` は 1b 違反が 1 行でもあれば
#: `ok=False` にする**行単位ゼロ許容**なので、整数配信だった場合 G-fin-1 は
#: **原理的に通らない**。それは本モジュールが葬った「1e-6 を全項目に課す」と
#: 同じ失敗である。**P5 の最初のタスク（実レスポンスでのキー存在確認）で
#: `eps`/`bps` の配信桁数を確定させ、その実測で 1% を裏づけるか、違反を
#: 件数（率）で見る形に変えるかを決めてから、この判定を切替の門にすること。**
DERIVATION_REL_TOL = 0.01

#: G-fin-1d の「新 writer だけ NULL」の上限比率。G-fin-2（カバレッジ ≥ 99.5%）と
#: 同じ 0.5% を項目単位で課す。
MAX_NEW_ONLY_NULL_RATIO = 0.005


@dataclass(frozen=True)
class FieldRule:
    """1 項目ぶんの判定規則。

    :param rel_tol: 旧⇔新の相対差の上限。`None` は「閾値を置かない＝観測のみ」で、
        その根拠はモジュール docstring の「閾値を置かなかった項目について」。
    :param require_sign_match: 符号の一致を必須にするか。TTM 改訂は大きさを
        変えるが符号は変えない。符号が反転しているなら会計基準か連結/単体の
        取り違えを疑う。
    :param evidence: その閾値（または閾値なし）を選んだ実測の根拠。
    """

    field: str
    rel_tol: float | None
    require_sign_match: bool
    evidence: str


FIELD_RULES: dict[str, FieldRule] = {
    "per": FieldRule(
        field="per",
        rel_tol=PUBLISHED_REL_TOL,
        require_sign_match=True,
        evidence="実測 3,317/3,317 一致・worst 1.313e-07。提供元の公表値を両 writer が写すだけ（仮説）",
    ),
    "pbr": FieldRule(
        field="pbr",
        rel_tol=PUBLISHED_REL_TOL,
        require_sign_match=True,
        evidence="実測 3,736/3,736 一致・worst 1.368e-07。per と同じ理由",
    ),
    "eps": FieldRule(
        field="eps",
        rel_tol=None,
        require_sign_match=True,
        evidence="実測 1,611 中 最厳 76 / 最緩 1,465 一致。TTM 改訂で値そのものが変わるため値一致は課さない",
    ),
    "bps": FieldRule(
        field="bps",
        rel_tol=None,
        require_sign_match=True,
        evidence="実測 1,612 中 648 → 949 一致。eps と同じ理由",
    ),
    "roe": FieldRule(
        field="roe",
        rel_tol=None,
        require_sign_match=True,
        evidence="実測 1,517 中 744 → 806 一致。eps/bps の商なので両方の改訂を受ける",
    ),
    "price": FieldRule(
        field="price",
        rel_tol=None,
        # price は符号一致を課さない。負の株価は存在しないので符号一致は常に真で、
        # 判定として何も言っていない。妥当性は G-fin-1c の正値チェックで見る。
        require_sign_match=False,
        evidence="実測 1,616 中 最厳 23 / 最緩 314 一致。日中に動く値なので取得時刻差がそのまま出る",
    ),
}

#: G-fin-1b の導出恒等式。(結果, 分子, 分母)。単位が揃っている組だけを入れる。
#: どれかが NULL / 0 の行は「検証不能」として数え、捏造で埋めない。
#:
#: **`roe = eps / bps` を入れていない。** ROE は「8.5」（%）で配信されることも
#: 「0.085」（比）で配信されることもあり、③断面テーブルのどちらなのかを
#: **実レスポンスで確かめていない**。ここに 100 倍の係数を推測で書けば、それは
#: 1e-6 を推測で置いたのと同じ誤りになる。P5 の最初のタスク（実レスポンスでの
#: キー存在確認）で単位を確定させてから足すこと。それまで `roe` は
#: G-fin-1c（符号）と分布の観測だけで見る。
DERIVED_IDENTITIES: tuple[tuple[str, str, str], ...] = (
    ("per", "price", "eps"),
    ("pbr", "price", "bps"),
)


# --- 比較の素 ---------------------------------------------------------------


def relative_difference(old: float | None, new: float | None) -> float | None:
    """相対差 |new - old| / max(|old|, |new|)。比較不能なら None。

    分母に `max(|old|, |new|)` を使うのは、`old` を分母にすると `old` が 0 に
    近い行で相対差が発散し、閾値の意味が行によって変わるため。片方だけが 0 の
    ときは 1.0（完全不一致）になり、両方 0 なら 0.0（一致）になる。
    """
    if old is None or new is None:
        return None
    scale = max(abs(float(old)), abs(float(new)))
    if scale == 0.0:
        return 0.0
    return abs(float(new) - float(old)) / scale


def _sign(value: float | None) -> int | None:
    if value is None:
        return None
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


@dataclass(frozen=True)
class FieldVerdict:
    """1 項目ぶんの判定結果。

    `observed_only=True` のとき `ok` は「値一致した」ではなく
    「**値一致は判定していない**」を意味する。読み替えを防ぐため、
    `summary()` にその旨を必ず書く。
    """

    field: str
    both_present: int  # 旧・新どちらにも値がある行数（= 項目単位の比較可能行）
    old_only_null: int
    new_only_null: int
    both_null: int
    matched: int | None  # rel_tol 以内の行数。閾値なしなら None
    sign_mismatch: int
    worst_rel_diff: float | None
    median_rel_diff: float | None
    p95_rel_diff: float | None
    problems: tuple[str, ...] = ()

    @property
    def observed_only(self) -> bool:
        return FIELD_RULES[self.field].rel_tol is None

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        rule = FIELD_RULES[self.field]
        head = (
            f"{self.field}: 比較可能 {self.both_present} 行"
            f" / 新のみ NULL {self.new_only_null} / 符号不一致 {self.sign_mismatch}"
        )
        if rule.rel_tol is None:
            return (
                f"{head} / **値一致は判定していない（観測のみ）**"
                f" 中央 {_fmt(self.median_rel_diff)} p95 {_fmt(self.p95_rel_diff)}"
                f" worst {_fmt(self.worst_rel_diff)}"
            )
        return (
            f"{head} / 一致 {self.matched} (rel_tol {rule.rel_tol:g})"
            f" worst {_fmt(self.worst_rel_diff)}"
        )


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3e}"


def _percentile(values: Sequence[float], q: float) -> float | None:
    """最近傍順位法の分位点。外部依存を増やさないために自前で持つ。"""
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def compare_field(
    field_name: str, pairs: Iterable[tuple[float | None, float | None]]
) -> FieldVerdict:
    """1 項目ぶんの旧⇔新を突き合わせる（G-fin-1a / 1d、および分布の観測）。

    `pairs` は「基準日が一致した行」の (旧値, 新値)。行単位の比較可能性は
    呼び出し側が切り、ここでは**項目単位**で NULL を数える（旧定義が行単位でしか
    比較不能を数えず、項目ごとに母数が 2 倍以上違うことを見えなくしていた）。
    """
    rule = FIELD_RULES[field_name]
    both_present = old_only_null = new_only_null = both_null = 0
    sign_mismatch = 0
    diffs: list[float] = []
    for old, new in pairs:
        if old is None and new is None:
            both_null += 1
            continue
        if new is None:
            new_only_null += 1
            continue
        if old is None:
            old_only_null += 1
            continue
        both_present += 1
        diff = relative_difference(old, new)
        if diff is not None:
            diffs.append(diff)
        if rule.require_sign_match and _sign(old) != _sign(new):
            sign_mismatch += 1

    matched = (
        sum(1 for d in diffs if d <= rule.rel_tol) if rule.rel_tol is not None else None
    )
    problems: list[str] = []
    if rule.rel_tol is not None and matched is not None and matched != len(diffs):
        problems.append(
            f"G-fin-1a {field_name}: 相対差 {rule.rel_tol:g} を超える行が"
            f" {len(diffs) - matched} 件 (worst {_fmt(max(diffs) if diffs else None)})"
            f" ／根拠: {rule.evidence}"
        )
    if sign_mismatch:
        problems.append(
            f"G-fin-1c {field_name}: 符号が反転した行が {sign_mismatch} 件。"
            " TTM 改訂では符号は変わらないので会計基準か連結/単体の取り違えを疑う"
        )
    denominator = both_present + new_only_null
    if denominator and new_only_null / denominator > MAX_NEW_ONLY_NULL_RATIO:
        problems.append(
            f"G-fin-1d {field_name}: 新 writer だけ NULL が {new_only_null}/"
            f"{denominator} 件で上限 {MAX_NEW_ONLY_NULL_RATIO:.1%} 超"
        )
    return FieldVerdict(
        field=field_name,
        both_present=both_present,
        old_only_null=old_only_null,
        new_only_null=new_only_null,
        both_null=both_null,
        matched=matched,
        sign_mismatch=sign_mismatch,
        worst_rel_diff=max(diffs) if diffs else None,
        median_rel_diff=median(diffs) if diffs else None,
        p95_rel_diff=_percentile(diffs, 0.95),
        problems=tuple(problems),
    )


# --- G-fin-1b / 1c: 1 スナップショット内の自己整合 ---------------------------


def check_row_consistency(row: dict[str, float | None]) -> list[str]:
    """1 行（= 同一取得バッチの 1 銘柄）の自己整合を見る（G-fin-1b / 1c）。

    writer 間の一致を見ないので TTM 改訂に影響されない。ここで見たいのは
    **桁・符号・単位**の誤りで、精度ではない。
    """
    problems: list[str] = []

    # G-fin-1c 符号・桁の矛盾。
    eps, per, pbr, bps, price = (
        row.get("eps"),
        row.get("per"),
        row.get("pbr"),
        row.get("bps"),
        row.get("price"),
    )
    if eps is not None and eps < 0 and per is not None and per > 0:
        problems.append(
            f"G-fin-1c: eps={eps} が負なのに per={per} が正。"
            " 赤字銘柄の PER は NULL か負でなければならない（符号を落としている）"
        )
    if bps is not None and bps <= 0 and pbr is not None and pbr > 0:
        problems.append(
            f"G-fin-1c: bps={bps} が正でないのに pbr={pbr} が正。"
            " 債務超過の PBR は NULL でなければならない"
        )
    if price is not None and price <= 0:
        problems.append(f"G-fin-1c: price={price}。株価は正でなければならない")

    # G-fin-1b 導出整合。分母が 0 / NULL の行は検証不能として黙って飛ばす
    # （捏造で埋めない。§3-1）。
    for result_name, numerator_name, denominator_name in DERIVED_IDENTITIES:
        result = row.get(result_name)
        numerator = row.get(numerator_name)
        denominator = row.get(denominator_name)
        if result is None or numerator is None or denominator in (None, 0):
            continue
        expected = float(numerator) / float(denominator)  # type: ignore[arg-type]
        diff = relative_difference(expected, result)
        if diff is not None and diff > DERIVATION_REL_TOL:
            problems.append(
                f"G-fin-1b: {result_name}={result} が同一バッチの"
                f" {numerator_name}/{denominator_name}={expected:.6g} と"
                f" 相対差 {diff:.3e}（上限 {DERIVATION_REL_TOL:g}）。"
                " 桁・単位の取り違えを疑う"
            )
    return problems


# --- まとめ -----------------------------------------------------------------


@dataclass(frozen=True)
class ParityReport:
    """G-fin-1 全体の判定。`ok` が True でも「値が一致した」ではない。"""

    verdicts: tuple[FieldVerdict, ...]
    row_problems: tuple[str, ...] = ()
    incomparable_rows: int = 0
    observed_only_fields: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.row_problems and all(v.ok for v in self.verdicts)

    def lines(self) -> list[str]:
        out = [v.summary() for v in self.verdicts]
        out.append(f"基準日が一致せず比較しなかった行: {self.incomparable_rows}")
        if self.observed_only_fields:
            out.append(
                "**値一致を判定していない項目**: "
                + " / ".join(self.observed_only_fields)
                + "（TTM 改訂で原理的に一致しない。閾値は3回連続の実測分布が出てから入れる）"
            )
        out.extend(self.row_problems)
        for verdict in self.verdicts:
            out.extend(verdict.problems)
        return out


def evaluate_parity(
    old_rows: dict[str, dict[str, float | None]],
    new_rows: dict[str, dict[str, float | None]],
    *,
    base_dates: dict[str, tuple[object, object]] | None = None,
) -> ParityReport:
    """旧 writer と新 writer の断面を突き合わせる（G-fin-1 全体）。

    :param old_rows: 銘柄コード -> {項目: 値}（旧 writer が書いた行）
    :param new_rows: 同・新 writer
    :param base_dates: 銘柄コード -> (旧の基準日, 新の基準日)。与えた場合、
        基準日が一致しない銘柄は比較から外して `incomparable_rows` に数える
        （旧定義の但し書きと同じ）。

    比較可能性を**行単位でしか切らない**のが旧定義の欠陥だったので、
    行を落としたうえで、さらに項目単位で NULL を数える。
    """
    codes = sorted(set(old_rows) & set(new_rows))
    comparable: list[str] = []
    incomparable = len(set(old_rows) | set(new_rows)) - len(codes)
    for code in codes:
        if base_dates is not None:
            pair = base_dates.get(code)
            if pair is None or pair[0] != pair[1]:
                incomparable += 1
                continue
        comparable.append(code)

    verdicts = tuple(
        compare_field(
            name,
            [(old_rows[c].get(name), new_rows[c].get(name)) for c in comparable],
        )
        for name in FIELD_RULES
    )
    row_problems: list[str] = []
    for code in comparable:
        for problem in check_row_consistency(new_rows[code]):
            row_problems.append(f"{code} {problem}")
    return ParityReport(
        verdicts=verdicts,
        row_problems=tuple(row_problems),
        incomparable_rows=incomparable,
        observed_only_fields=tuple(
            name for name, rule in FIELD_RULES.items() if rule.rel_tol is None
        ),
    )
