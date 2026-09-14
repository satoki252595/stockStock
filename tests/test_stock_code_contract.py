"""銘柄コード契約の言語横断テスト (docs/CONTRACTS.md)。

期待値は `tests/fixtures/contracts/stock-code-vectors.json` にあり、同一バイト列の
ファイルを kabulab-cf (TypeScript) 側のテストも読む。**この JSON を直すと相手
リポジトリの実装も直す必要がある**（CI の cross-repo-contract ジョブが両リポの
JSON を diff する）。

ここで押さえるのは 3 操作の区別:

- normalize         … 表現揺れの吸収のみ（妥当性は見ない）
- parse             … 4 文字の正準形か（① 銘柄マスタ由来の値の検証に使う）
- source_to_ticker  … TDnet/EDINET の 5 文字形式 → 4 文字ティッカー
- margin_to_key     … JPX 信用残 PDF の 5 文字形式 → rows[].code（種類株は 5 文字のまま）

この 3 つを 1 つの関数で兼ねようとして 7 実装が割れていた。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jp_stock_pipeline.collectors.edinet_codelist import normalize_sec_code
from jp_stock_pipeline.collectors.tdnet_yanoshin import normalize_company_code
from jp_stock_pipeline.contracts.stock_code import (
    STOCK_CODE_RE,
    is_valid_stock_code,
    margin_code_to_key,
    normalize_stock_code,
    parse_stock_code,
    source_code_to_ticker,
)
VECTORS_PATH = (
    Path(__file__).parent / "fixtures" / "contracts" / "stock-code-vectors.json"
)
_FIXTURE = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
VECTORS = _FIXTURE["vectors"]


def _ids() -> list[str]:
    return [v["id"] for v in VECTORS]


class TestSharedVectorFile:
    def test_loads_and_is_not_trivial(self):
        assert len(VECTORS) > 15

    def test_canonical_pattern_matches_declaration(self):
        """パターン自体を両言語で固定する。

        `\\d` ではなく `[0-9]` で書いているのは、TypeScript の正規表現リテラルの
        `.source` と 1 文字ずつ一致させるため（かつ Python の `\\d` は Unicode
        数字も拾うので意味も違う）。
        """
        assert STOCK_CODE_RE.pattern == _FIXTURE["canonical_regex"]

    def test_ids_are_unique(self):
        ids = _ids()
        assert len(set(ids)) == len(ids)


@pytest.mark.parametrize("vector", VECTORS, ids=_ids())
class TestCanonicalImplementation:
    def test_normalize(self, vector):
        assert normalize_stock_code(vector["input"]) == vector["normalize"]

    def test_parse(self, vector):
        assert parse_stock_code(vector["input"]) == vector["parse"]

    def test_is_valid_agrees_with_parse(self, vector):
        assert is_valid_stock_code(vector["input"]) is (vector["parse"] is not None)

    def test_source_to_ticker(self, vector):
        assert source_code_to_ticker(vector["input"]) == vector["source_to_ticker"]

    def test_margin_to_key(self, vector):
        assert margin_code_to_key(vector["input"]) == vector["margin_to_key"]

    def test_margin_to_key_never_collides_with_a_different_ticker(self, vector):
        """4 文字を返すなら source_to_ticker と同じ値。それ以外は 4 文字にしない。

        信用残で種類株を普通株のコードへ潰さない (取り違え) ことの一般形。
        """
        key = margin_code_to_key(vector["input"])
        if key is not None and len(key) == 4:
            assert key == vector["source_to_ticker"]


@pytest.mark.parametrize("vector", VECTORS, ids=_ids())
class TestCallSitesDelegate:
    """取込 2 系統が共有実装へ委譲しきっていることを公開 API 側から確認する。

    片方だけ独自実装に戻ると落ちる。
    """

    def test_tdnet_company_code(self, vector):
        assert normalize_company_code(vector["input"]) == vector["source_to_ticker"]

    def test_edinet_sec_code(self, vector):
        assert normalize_sec_code(vector["input"]) == vector["source_to_ticker"]
