"""公開リポジトリに置いてはいけないフィクスチャの追跡を禁じる。

このリポジトリは PUBLIC。`tests/fixtures/*/README.md` は JPX を
「公開・再配布・商用利用を禁止する」、日証金を「第三者の利用に供することを
固く禁じます」、TDnet 開示を「再配布・転載はしない」と自ら明記している。
それらを commit すると、リポジトリ自身がその禁止を破ることになる
（2026-09-12 に該当 17 ファイルを履歴ごと除去した）。

`.gitignore` だけでは `git add -f` を止められないので、追跡状態をここで固定する。
"""

from __future__ import annotations

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
        # 取得失敗時の応答断片。stooq のデータそのものではない
        "tests/fixtures/prices/stooq_challenge_response.html",
        # 銘柄コード契約の言語横断テストベクタ（kabulab-cf と同一バイト列で共有）。
        # 取得物ではなく手書きの契約定義で、収録しているのは境界値のコード文字列
        # （大半は "07203" / "A130" / "1234567" のような合成値）と説明文だけ。
        # personal-only 列（market / sector / sector17 / instrument_type /
        # license_tag / src_source / quality）の値は 1 つも含まない
        # （`sector33` はここに挙げない。EDINET「提出者業種」由来の
        #  commercial-ok 列である。`cloud_store/schema.py` の
        #  MIXED_LICENSE_COLUMNS が正本）。
        # JPX/TDnet/日証金のデータセットの再配布にはあたらないので追跡してよい。
        "tests/fixtures/contracts/stock-code-vectors.json",
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
