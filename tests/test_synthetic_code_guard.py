"""銘柄コード契約フィクスチャが「合成コードは 1300 未満」の規約を守っているかの
静的検査 (kabulab-cf `src/shared/jpx/synthetic-code-guard.test.ts` の Python 側対)。

対象は 2 つ:

- `tests/fixtures/contracts/stock-code-vectors.json`（両リポ共有・CI
  cross-repo-contract の突合対象。`test_stock_code_contract.py` が実装の入出力を
  検証する側で、こちらはフィクスチャの値そのものを検査する）
- `tests/test_jpx_margin.py`（kabulab-cf `services/vwap-analysis/lib/margin.test.ts`
  と手動で値を揃えている合成 PDF フィクスチャ）

この 2 つが「種類株契約の根拠として実在コードを意図的に使う」場所そのものなので、
許容リストもこの契約例（先頭 4 桁）に絞る。1300 未満は合成コードの領域として
無条件に許容する（JPX 未割当であることを実測済み: 配信用 stocks.json の最小
コードは 1301、D1 core_stocks の MIN(code) も 1301）。新しく 1300 以上の
数字コードをフィクスチャに足すときは、この 2 ファイル以外では合成コード
（1000〜1299）を使うこと。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VECTORS_PATH = REPO_ROOT / "tests" / "fixtures" / "contracts" / "stock-code-vectors.json"
MARGIN_TEST_PATH = REPO_ROOT / "tests" / "test_jpx_margin.py"

# 許容する実在コードの先頭 4 桁。この 2 ファイルが種類株契約の根拠として使っている
# 「普通株の基本例 + それに紐づく優先株・種類株」の組だけを載せる。区分の語や会社名は
# ここに書かない（許容リストは数値だけを見る）。
ALLOWED_REAL_PREFIXES = frozenset({7203, 2593, 9434})

_NUMERIC_CODE_RE = re.compile(r"^\d{4,5}$")


def violates_synthetic_code_policy(code: str) -> bool:
    """数字のみ・4〜5 文字のコードで、先頭 4 桁が 1300 以上かつ許容リスト外なら True。"""
    if not _NUMERIC_CODE_RE.match(code):
        return False  # 英字混じりコード (130A 系) は対象外
    prefix = int(code[:4])
    if prefix < 1300:
        return False
    return prefix not in ALLOWED_REAL_PREFIXES


VECTOR_FIELDS = ("input", "normalize", "parse", "source_to_ticker", "margin_to_key")


def _load_vectors() -> list[dict]:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]


def _vector_field_violations() -> list[str]:
    offenders: list[str] = []
    for vector in _load_vectors():
        for field in VECTOR_FIELDS:
            value = vector.get(field)
            if isinstance(value, str) and violates_synthetic_code_policy(value):
                offenders.append(f'{field}="{value}"')
    return offenders


# tests/test_jpx_margin.py の `"code": "…"` を拾う (辞書キー code のみ)。
_CODE_ASSIGNMENT_RE = re.compile(r'"code":\s*"(\d{4,5})"')


def _margin_test_code_violations() -> list[str]:
    source = MARGIN_TEST_PATH.read_text(encoding="utf-8")
    return [
        match.group(1)
        for match in _CODE_ASSIGNMENT_RE.finditer(source)
        if violates_synthetic_code_policy(match.group(1))
    ]


class TestViolatesSyntheticCodePolicy:
    """検出器そのものの単体テスト。"""

    def test_1300未満は合成コードの領域として許容する(self) -> None:
        assert violates_synthetic_code_policy("1299") is False
        assert violates_synthetic_code_policy("1202") is False
        assert violates_synthetic_code_policy("12024") is False

    def test_1300以上は許容リストに無ければfail(self) -> None:
        assert violates_synthetic_code_policy("1300") is True
        assert violates_synthetic_code_policy("4001") is True
        assert violates_synthetic_code_policy("40015") is True

    def test_先頭4桁が許容リストにあれば検査文字によらず許容する(self) -> None:
        assert violates_synthetic_code_policy("7203") is False
        assert violates_synthetic_code_policy("72030") is False
        assert violates_synthetic_code_policy("25935") is False
        assert violates_synthetic_code_policy("94346") is False

    def test_英字混じりコードは対象外(self) -> None:
        assert violates_synthetic_code_policy("130A") is False
        assert violates_synthetic_code_policy("130A5") is False

    def test_先頭0は1300未満として扱う(self) -> None:
        assert violates_synthetic_code_policy("07203") is False


class TestSharedFixturesFollowSyntheticCodePolicy:
    def test_走査対象が空振りしていない(self) -> None:
        assert len(_load_vectors()) > 15
        source = MARGIN_TEST_PATH.read_text(encoding="utf-8")
        assert len(_CODE_ASSIGNMENT_RE.findall(source)) > 3

    def test_stock_code_vectors_jsonに許容リスト外の1300以上コードが無い(self) -> None:
        assert _vector_field_violations() == []

    def test_test_jpx_margin_pyのcodeに許容リスト外の1300以上コードが無い(self) -> None:
        assert _margin_test_code_violations() == []
