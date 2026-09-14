"""wrangler のデバッグログを既定でローカルに残さないことのテスト。

wrangler は既定で `~/Library/Preferences/.wrangler/logs`（xdg config）に
D1 応答をそのまま書く。優待の掲載文など規約上再配布できない本文が
含まれうるため、devShell に入った時点で無効化しておく
（実測 2026-09-14: 同ディレクトリに 33,096 本・620MB が既に蓄積していた）。

固定するのは flake.nix の shellHook が次の 2 点を満たすこと:
- `WRANGLER_WRITE_LOGS=false` でディスク書込そのものを止める
- `WRANGLER_LOG_PATH` をリポジトリ内 (gitignore 済み) の `.wrangler/logs` に
  向け、デバッグで `WRANGLER_WRITE_LOGS=true` を付けたときも既定の
  `~/Library` へ書かせない

`nix develop` を実際に起動する検証はしない（CI・ローカルとも低速で、
この 2 行が消えたことだけを検出したいなら不要）。実機確認は
`nix develop -c sh -c 'echo $WRANGLER_WRITE_LOGS $WRANGLER_LOG_PATH'` で行う。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _shell_hook() -> str:
    flake = (REPO_ROOT / "flake.nix").read_text(encoding="utf-8")
    match = re.search(r"shellHook\s*=\s*''(.*?)'';", flake, re.DOTALL)
    assert match, "flake.nix に shellHook が見つからない"
    return match.group(1)


class TestWranglerWriteLogsDisabled:
    def test_既定でディスクへ書かない(self) -> None:
        assert "export WRANGLER_WRITE_LOGS=false" in _shell_hook()

    def test_UV_PYTHON_など既存の設定を壊していない(self) -> None:
        """shellHook を丸ごと差し替える変更をしたら壊れる、既存機能の回帰チェック。"""
        hook = _shell_hook()
        assert "export UV_PYTHON=" in hook
        assert "export UV_PYTHON_DOWNLOADS=never" in hook


class TestWranglerLogPathIsRepoLocal:
    def test_ログ置き場をリポジトリ内に向けている(self) -> None:
        hook = _shell_hook()
        assert "export WRANGLER_LOG_PATH=" in hook

    def test_既定のホームディレクトリ配下を指していない(self) -> None:
        """`~/Library/Preferences/.wrangler/logs`（xdg config の既定値）を
        直接指す退行を防ぐ。値は $(git rev-parse --show-toplevel) 等の
        リポジトリ相対パスであるべきで、$HOME や ~ を含んではいけない。
        """
        hook = _shell_hook()
        log_path_line = next(
            line for line in hook.splitlines() if "WRANGLER_LOG_PATH=" in line
        )
        assert "$HOME" not in log_path_line
        assert "~" not in log_path_line
        assert ".wrangler/logs" in log_path_line


class TestGitignoreCoversWranglerLogs:
    def test_wrangler_ディレクトリを追跡しない(self) -> None:
        gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        patterns = {line.strip() for line in gitignore.splitlines()}
        assert ".wrangler/" in patterns
