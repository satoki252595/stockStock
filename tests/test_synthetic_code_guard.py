"""銘柄コード契約フィクスチャが「合成コードは 1300 未満」の規約を守っているかの
静的検査 (kabulab-cf `src/shared/jpx/synthetic-code-guard.test.ts` の Python 側対)。

対象は 2 系統:

(1) `tests/fixtures/contracts/stock-code-vectors.json`（両リポ共有・CI
    cross-repo-contract の突合対象。`test_stock_code_contract.py` が実装の入出力を
    検証する側で、こちらはフィクスチャの値そのものを検査する）と
    `tests/test_jpx_margin.py`（kabulab-cf `services/vwap-analysis/lib/margin.test.ts`
    と手動で値を揃えている合成 PDF フィクスチャ）。この 2 つが「種類株契約の根拠と
    して実在コードを意図的に使う」場所そのものなので、許容リストもこの契約例
    （先頭 4 桁）に絞る。
(2) 「合成コード」と注記したコード例 — `tests/test_tdnet_yanoshin.py` /
    `tests/test_universe_guards.py` / `src/jp_stock_pipeline/collectors/tdnet_yanoshin.py`
    / `src/jp_stock_pipeline/contracts/stock_code.py` の docstring・コメント。
    区分値の形は取らないが、注記した値そのものが規約に違反していたら
    (= 実は合成でない) fail する。注記だけ残してコードを実在値へ書き戻す
    (雑な revert) を検出する。「同じ行」「直前から連続するコメント/docstring
    ブロック + それに続く 1 行」のどちらかで注記とコードが揃っていれば検査対象
    にする (このリポの対象ファイルは配列的な複数行フィクスチャを持たないので、
    kabulab-cf 側のようなオブジェクトリテラル単位の判定は不要)。

どちらも 1300 未満は合成コードの領域として無条件に許容する（JPX 未割当であることを
実測済み: 配信用 stocks.json の最小コードは 1301、D1 core_stocks の MIN(code) も
1301）。新しく 1300 以上の数字コードをフィクスチャに足すときは、上記の契約例
以外では合成コード（1000〜1299）を使うこと。
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


# --- 「合成コード」と注記した値の整合性チェック ----------------------------------

# 「合成コード」の注記。区分値と違い自然文なので "合成" の有無だけを見る。
_SYNTHETIC_MARKER_RE = re.compile(r"合成")
# クォート・バッククォート付きの 4〜5 桁の数字コード。
_CODE_LITERAL_RE = re.compile(r"[`\"](\d{4,5})[`\"]")

SYNTHETIC_MARKER_SCAN_TARGETS = (
    REPO_ROOT / "tests" / "test_tdnet_yanoshin.py",
    REPO_ROOT / "tests" / "test_universe_guards.py",
    REPO_ROOT / "src" / "jp_stock_pipeline" / "collectors" / "tdnet_yanoshin.py",
    REPO_ROOT / "src" / "jp_stock_pipeline" / "contracts" / "stock_code.py",
)


def _comment_block_ids(lines: list[str]) -> list[int]:
    """各行にコメントブロック ID を振る。連続するコメント行 (`#`)・docstring
    (`\"\"\"` で開いてから閉じるまでの全行、開閉行自身を含む) は同じ ID を保ち、
    空行またはコード行を 1 つ消費すると次の行から新しい ID になる (直前の
    コメント/docstring とそれに続く 1 行だけを 1 単位にし、無関係な離れた行や
    配列的な複数行フィクスチャの兄弟行が混ざらないようにする)。

    `\"\"\"` の出現回数の偶奇で docstring の開始・終了を追う単純なトラッカー
    (対象ファイルはトリプルクォートを 1 行に 2 つ以上書く一行 docstring と
    複数行 docstring のどちらもあるが、いずれも `\"\"\"` の対を跨いだ入れ子は無い)。
    """
    ids: list[int] = []
    block_id = 0
    in_docstring = False
    for line in lines:
        quote_count = line.count('"""')
        is_doc_or_comment = in_docstring or line.strip().startswith("#")
        if quote_count % 2 == 1:
            is_doc_or_comment = True
            in_docstring = not in_docstring
        elif quote_count > 0:
            is_doc_or_comment = True  # 1 行完結の """…""" docstring
        if line.strip() == "" and not in_docstring:
            ids.append(block_id)
            block_id += 1
            continue
        ids.append(block_id)
        if not is_doc_or_comment:
            block_id += 1
    return ids


def _find_marker_code_violations(source: str) -> list[str]:
    """`source` の中で「合成」の注記と実際のコードが「同じ行」または「直前の
    コメント/docstring ブロック + それに続く 1 行」のどちらかで揃っており、
    かつそのコードが規約に違反する場合に `"<行番号>: <コード>"` を返す。
    """
    lines = source.split("\n")
    comment_ids = _comment_block_ids(lines)

    marker_lines: set[int] = set()
    marker_blocks: set[int] = set()
    for i, line in enumerate(lines):
        if _SYNTHETIC_MARKER_RE.search(line):
            marker_lines.add(i)
            marker_blocks.add(comment_ids[i])

    offenders: list[str] = []
    for i, line in enumerate(lines):
        for match in _CODE_LITERAL_RE.finditer(line):
            code = match.group(1)
            if not violates_synthetic_code_policy(code):
                continue
            if i in marker_lines or comment_ids[i] in marker_blocks:
                offenders.append(f"{i + 1}: {code}")
    return offenders


def _synthetic_marker_violations(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    return [f"{path.name}:{v}" for v in _find_marker_code_violations(source)]


class TestSyntheticMarkerMatchesPolicy:
    def test_走査対象が空振りしていない(self) -> None:
        for path in SYNTHETIC_MARKER_SCAN_TARGETS:
            assert _SYNTHETIC_MARKER_RE.search(path.read_text(encoding="utf-8")), path

    def test_注記のある行から離れた_続く文のコードも拾う(self) -> None:
        source = "\n".join(
            [
                '    """仕様変更。以前は "12024" == "1202" を要求していた。',
                "",
                "    - `1202` は合成コードで core_stocks に不在。",
                '    """',
                '    assert ty.normalize_company_code("40015") is None',
            ]
        )
        assert _find_marker_code_violations(source) == ["5: 40015"]

    def test_無関係な手前のブロックには波及しない(self) -> None:
        source = "\n".join(
            [
                "def test_別のテスト():",
                '    assert f("6549") == "6549"',
                "",
                "def test_合成コードのテスト():",
                "    # 合成コード 1202",
                '    assert f("12024") is None',
            ]
        )
        assert _find_marker_code_violations(source) == []

    def test_scan_targetsに違反が無い(self) -> None:
        for path in SYNTHETIC_MARKER_SCAN_TARGETS:
            assert _synthetic_marker_violations(path) == []
