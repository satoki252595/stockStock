"""公開リポジトリに置いてはいけないフィクスチャの追跡を禁じる。

このリポジトリは PUBLIC。`tests/fixtures/*/README.md` は JPX を
「公開・再配布・商用利用を禁止する」、日証金を「第三者の利用に供することを
固く禁じます」、TDnet 開示を「再配布・転載はしない」と自ら明記している。
それらを commit すると、リポジトリ自身がその禁止を破ることになる
（2026-09-12 に該当 17 ファイルを履歴ごと除去した）。

`.gitignore` だけでは `git add -f` を止められないので、追跡状態をここで固定する。
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# 追跡してよいもの: データを含まない説明・ポインタと、commercial-ok の EDINET。
ALLOWED: frozenset[str] = frozenset(
    {
        "tests/fixtures/convert/README.md",
        "tests/fixtures/convert/tdnet_disclosure_sample.source.txt",
        "tests/fixtures/jpx/README.md",
        "tests/fixtures/jsf/README.md",
        # EDINET は commercial-ok（公共データ利用規約準拠 §2.1）
        "tests/fixtures/edinet/Edinetcode.zip",
        "tests/fixtures/edinet/documents_error_401.json",
        # 銘柄コード契約の言語横断テストベクタ（kabulab-cf と同一バイト列で共有）。
        # 取得物ではなく手書きの契約定義で、収録しているのは境界値のコード文字列
        # （大半は "07203" / "A130" / "1234567" のような合成値）と説明文だけ。
        # personal-only 列（`market` / `sector` / `sector17` /
        # `instrument_type`）の値は 1 つも含まない。正本は
        # `cloud_store/schema.py` の MIXED_LICENSE_COLUMNS で、そこに挙げて
        # いない列を推測でここへ書かない（`sector33` は EDINET「提出者業種」
        # 由来の commercial-ok 列、`license_tag` / `src_source` /
        # `src_data_date` / `src_fetched_at` / `quality` は第三者由来の値を
        # 含まない来歴メタなので commercial-ok。どちらも personal-only ではない）。
        # JPX/TDnet/日証金のデータセットの再配布にはあたらないので追跡してよい。
        "tests/fixtures/contracts/stock-code-vectors.json",
        # D1 の列単位ライセンス地図と表区分の言語横断契約（同上・同一バイト列で共有）。
        # 収録しているのは**スキーマのメタデータ**（表名・列名・ライセンスタグ）と
        # 説明文だけで、JPX/日証金/TDnet/みんかぶの**データ値は 1 つも含まない**。
        # 列名 `market` / `sector` は personal-only 列の**名前**であって値ではない
        # （名前を伏せると公開面のフィルタを両リポジトリで共有できず、
        #  地図を 1 つにするという目的自体が達成できない）。
        "tests/fixtures/contracts/d1-license-map.json",
    }
)


def _tracked_fixtures() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "tests/fixtures/"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"git で追跡状態を取れない: {exc}")
    return [line for line in out.splitlines() if line.strip()]


def test_規約上公開できないフィクスチャを追跡していない() -> None:
    unexpected = sorted(set(_tracked_fixtures()) - ALLOWED)
    assert not unexpected, (
        "公開リポジトリに置けないフィクスチャが追跡されています: "
        f"{unexpected}\n"
        "取得は scripts/capture_*.py で行い、手元にだけ置いてください"
        "（未取得なら該当テストは skip します）"
    )


def test_許可リストが実在するファイルだけを指す() -> None:
    """許可リストが古くなって意味を失っていないことを確かめる。"""
    missing = sorted(p for p in ALLOWED if not (REPO_ROOT / p).exists())
    assert not missing, f"許可リストに実在しないパスがある: {missing}"


@pytest.mark.parametrize(
    "path",
    [
        "tests/fixtures/convert/data_j.xlsx",
        "tests/fixtures/jsf/zandaka.csv",
        "tests/fixtures/tdnet/sample.pdf",
        "tests/fixtures/jpx/syumatsu_weekly_20260904.txt",
        "tests/fixtures/transform/yfinance_7203T_info.json",
    ],
)
def test_gitignore_が再取得物を除外する(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", path], cwd=REPO_ROOT, check=False
    )
    assert result.returncode == 0, f"{path} が .gitignore で除外されていない"


def test_EDINET_は除外しない() -> None:
    """commercial-ok なので追跡を続ける。ignore 規則の巻き込みを検出する。"""
    result = subprocess.run(
        ["git", "check-ignore", "-q", "tests/fixtures/edinet/Edinetcode.zip"],
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode != 0, "EDINET 原本まで ignore されている"


# skip を許す fixture_path の一覧（L-28）。未取得なら該当テストは skip する。
# 新しい実レスポンス依存を足すときはここへ足すこと（黙って増やさない）。
# f-string は `{…}` に正規化する（`jsf/{name}.csv` → `jsf/{…}.csv`）。
SKIP_ALLOWED: frozenset[str] = frozenset(
    {
        "convert/tdnet_disclosure_sample.pdf",
        "convert/yanoshin_tdnet_recent.json",
        "edinet/Edinetcode.zip",
        "edinet/csv_sample.meta.json",
        "edinet/csv_sample.zip",
        "edinet/documents_error_401.json",
        "edinet/documents_list_sample.json",
        "edinet/xbrl_sample.meta.json",
        "edinet/xbrl_sample.zip",
        "jsf/shina.csv",
        "jsf/zandaka.csv",
        "jsf/{…}.csv",
        "tdnet/official_I_list_{…}_20260610.html",
        "tdnet/tanshin_xbrl_2751_20260610.zip",
        "tdnet/yanoshin_list_20260610.json",
        "tdnet/yanoshin_list_recent.json",
    }
)

# fixture_path へ渡す値を保持するモジュール定数（動的呼び出しの解決用）。
# 新しい定数経由の参照を足すときはここへ足すこと。
_FIXTURE_CONSTANTS = ("FIXTURE_REL", "PDF_REL", "ZIP_FIXTURE")

# fixture_path を包むヘルパー（引数をそのまま渡すもの）。呼び出し側のリテラル
# を拾うために名前を固定する。`_bytes(name)` のように名前だけ受けるものは
# 含めない（`fixture_path(f"jsf/{name}.csv")` の f-string 側で拾う）。
_FIXTURE_WRAPPERS = ("_load", "_doc_meta")


def _rendered_joined(node: ast.JoinedStr) -> str:
    return "".join(
        v.value if isinstance(v, ast.Constant) else "{…}" for v in node.values
    )


def _fixture_refs() -> set[str]:
    """テスト群が参照する fixture_path の集合を AST で集める。"""
    refs: set[str] = set()
    for path in sorted(REPO_ROOT.glob("tests/test_*.py")):
        if path.name == "test_fixture_policy.py":
            continue
        tree = ast.parse(path.read_text())
        constants: dict[str, str] = {}
        for node in tree.body:
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in _FIXTURE_CONSTANTS
                and isinstance(node.value, ast.Constant)
            ):
                constants[node.targets[0].id] = node.value.value
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else ""
            if name not in ("fixture_path", *_FIXTURE_WRAPPERS):
                continue
            if not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                refs.add(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                refs.add(_rendered_joined(arg))
            elif isinstance(arg, ast.Name) and arg.id in constants:
                refs.add(constants[arg.id])
    return refs


def test_skipを許すフィクスチャ以外を参照していない() -> None:
    """実レスポンス依存（= 未取得で skip）の増加を検出する（L-28）。"""
    refs = _fixture_refs()
    assert refs, "参照を 1 件も拾えていない（抽出ロジックの故障）"
    unknown = sorted(refs - SKIP_ALLOWED)
    assert not unknown, (
        f"SKIP_ALLOWED に無いフィクスチャ参照: {unknown}\n"
        "新しい実レスポンス依存は SKIP_ALLOWED へ追加してから使うこと"
    )
    stale = sorted(SKIP_ALLOWED - refs)
    assert not stale, f"参照されなくなった許可エントリ（掃除すること）: {stale}"
