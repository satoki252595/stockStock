"""設定: 環境変数 + db_ids.json から読み込む (DESIGN.md §9)。

- 資格情報は GitHub Secrets / 環境変数のみ。コードに埋め込まない
- DB ID は環境変数を優先し、無ければ db_ids.json（schema.py が出力）を読む
- Notion スロットル値は設定化 (§11)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

# 設計書 §1: Notion 親ページ「株式情報」
DEFAULT_PARENT_PAGE_ID = "205d74ff84cd809e92b4dd1adc5cf186"

DEFAULT_NOTION_RPS = 2.5  # §6.1

# DB論理キー → (環境変数名, Notion上のDBタイトル)
DB_REGISTRY: dict[str, tuple[str, str]] = {
    "stock_master": ("NOTION_DB_STOCK_MASTER", "① 銘柄マスタ"),
    "prices": ("NOTION_DB_PRICES", "② 株価テクニカル"),
    "financials": ("NOTION_DB_FINANCIALS", "③ 財務サマリ"),
    "disclosures": ("NOTION_DB_DISCLOSURES", "④ 開示書類"),
    "raw_files": ("NOTION_DB_RAW_FILES", "⑤ 原本ファイル"),
    "exports": ("NOTION_DB_EXPORTS", "⑥ 時系列エクスポート"),
    "job_log": ("NOTION_DB_JOB_LOG", "⑦ 収集ジョブログ"),
}


class ConfigError(RuntimeError):
    pass


# 端末B(ローカル PostgreSQL)既定値。IP/ユーザー等の機微情報は .env で上書きする。
DEFAULT_LOCAL_DB_PORT = 5432
DEFAULT_LOCAL_DB_NAME = "jp_stock"
DEFAULT_LOCAL_DB_LAN_HOST = "localhost"
# cloud: 経路を暗号化(require)。ただし require はサーバ認証をしない — 能動的 MITM
# 対策には VPN 経由か sslmode=verify-full + ルートCA が必要 (§7.1)。
DEFAULT_LOCAL_DB_SSLMODE = "require"
DEFAULT_LOCAL_DB_LAN_SSLMODE = "prefer"     # lan: 同一 LAN なので TLS 任意

# dual-write の接続プロファイル。--db-target で選択（既定 cloud）。
DB_TARGET_CLOUD = "cloud"
DB_TARGET_LAN = "lan"
DB_TARGETS = (DB_TARGET_CLOUD, DB_TARGET_LAN)


@dataclass
class LocalStoreSettings:
    """端末B(PostgreSQL)への dual-write / FastAPI 配信の接続情報 (.env 管理)。

    接続プロファイルは2系統で、収集ジョブの --db-target で選ぶ:
    - cloud(既定): host=LOCAL_DB_HOST(端末B のグローバル/VPN アドレス) + sslmode=require。
      クラウド(GitHub Actions 等)から端末B へ格納する既定経路。
    - lan: lan_host=LOCAL_DB_LAN_HOST(既定 localhost) + sslmode=prefer。同一 LAN で
      手動実行するとき --db-target lan で選ぶ。FastAPI(端末B 内)も lan で localhost 接続。

    対象プロファイルの host が未設定なら dual-write は無効（Notion のみ書き込む）。
    資格情報はコードに埋め込まず LOCAL_DB_* 環境変数 (.env) からのみ読む (§9)。
    """

    host: str | None            # cloud 接続先 (LOCAL_DB_HOST)
    port: int
    dbname: str
    user: str | None
    password: str | None
    api_key: str | None  # FastAPI の X-API-Key 認証用 (API 側のみ参照)
    lan_host: str | None = None                        # lan 接続先 (LOCAL_DB_LAN_HOST。未設定=無効)
    sslmode: str = DEFAULT_LOCAL_DB_SSLMODE            # cloud の TLS モード
    lan_sslmode: str = DEFAULT_LOCAL_DB_LAN_SSLMODE    # lan の TLS モード

    def _host_for(self, target: str) -> str | None:
        """dual-write 有効判定用の host（明示設定値。lan も未設定なら None）。"""
        return self.lan_host if target == DB_TARGET_LAN else self.host

    def enabled(self, target: str = DB_TARGET_CLOUD) -> bool:
        """その接続プロファイルの host が明示設定されていれば dual-write を有効化する。

        cloud は LOCAL_DB_HOST、lan は LOCAL_DB_LAN_HOST が必要。lan を localhost へ
        暗黙フォールバックして意図しない自端末への誤ミラーを起こさないよう、lan も
        host の明示を要求する（API の自DB接続は connect_kwargs 側で localhost を補完）。
        """
        return bool(self._host_for(target))

    def connect_kwargs(self, target: str = DB_TARGET_CLOUD) -> dict:
        """psycopg.connect(**kwargs) 用の接続引数。

        文字列連結ではなくキーワード引数で渡す（空白・特殊文字を含むパスワードでも
        libpq のクォート不備で壊れない）。target=lan で lan_host 未設定なら localhost に
        補完する（端末B 内 FastAPI の自DB接続用）。sslmode=require は経路を暗号化するが
        サーバ認証はしない（能動的 MITM 対策には VPN か verify-full + ルートCA が必要 §7.1）。
        """
        if target == DB_TARGET_LAN:
            host = self.lan_host or DEFAULT_LOCAL_DB_LAN_HOST
            sslmode = self.lan_sslmode
        else:
            host = self.host
            sslmode = self.sslmode
        kwargs: dict = {"host": host, "port": self.port, "dbname": self.dbname, "sslmode": sslmode}
        if self.user:
            kwargs["user"] = self.user
        if self.password:
            kwargs["password"] = self.password
        return kwargs


def _load_local_store(env: dict[str, str]) -> LocalStoreSettings:
    raw_port = (env.get("LOCAL_DB_PORT") or "").strip()
    try:
        port = int(raw_port) if raw_port else DEFAULT_LOCAL_DB_PORT
    except ValueError as exc:
        raise ConfigError(f"LOCAL_DB_PORT が不正: {raw_port!r}") from exc
    return LocalStoreSettings(
        host=env.get("LOCAL_DB_HOST") or None,
        port=port,
        dbname=env.get("LOCAL_DB_NAME") or DEFAULT_LOCAL_DB_NAME,
        user=env.get("LOCAL_DB_USER") or None,
        password=env.get("LOCAL_DB_PASSWORD") or None,
        api_key=env.get("LOCAL_API_KEY") or None,
        lan_host=env.get("LOCAL_DB_LAN_HOST") or None,
        sslmode=env.get("LOCAL_DB_SSLMODE") or DEFAULT_LOCAL_DB_SSLMODE,
        lan_sslmode=env.get("LOCAL_DB_LAN_SSLMODE") or DEFAULT_LOCAL_DB_LAN_SSLMODE,
    )


@dataclass
class Settings:
    notion_token: str | None
    notion_parent_page_id: str
    edinet_api_key: str | None
    notion_rps: float
    raw_data_dir: Path
    dry_run: bool
    # stooq は 2026年時点でブラウザ検証(PoW)を返し CSV を一切返さない＝実質死亡。
    # 既定 False で「死んだ第2ソース」を即 fast-fail し、PoW 計算と HTTP 往復を
    # 丸ごと省く(reconcile_weekly の 46分タイムアウト / prices_daily フォールバックの
    # 浪費を排除)。復活時は env STOOQ_ENABLED=true で従来挙動に戻る(コード変更不要)。
    stooq_enabled: bool = False
    db_ids: dict[str, str] = field(default_factory=dict)
    local_store: LocalStoreSettings = field(
        default_factory=lambda: LocalStoreSettings(
            host=None, port=DEFAULT_LOCAL_DB_PORT, dbname=DEFAULT_LOCAL_DB_NAME,
            user=None, password=None, api_key=None,
        )
    )

    def db_id(self, key: str) -> str:
        if key not in DB_REGISTRY:
            raise KeyError(f"未知のDBキー: {key} (有効: {sorted(DB_REGISTRY)})")
        if key not in self.db_ids:
            env_name, title = DB_REGISTRY[key]
            raise ConfigError(
                f"DB ID 未設定: {title}。環境変数 {env_name} を設定するか、"
                "`python -m jp_stock_pipeline.notion.schema` を実行して db_ids.json を生成すること"
            )
        return self.db_ids[key]


def _load_db_ids(env: dict[str, str]) -> dict[str, str]:
    db_ids: dict[str, str] = {}
    # 1) db_ids.json (schema.py の出力)
    path = Path(env.get("NOTION_DB_IDS_FILE", "db_ids.json"))
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in DB_REGISTRY:
            if isinstance(data.get(key), str):
                db_ids[key] = data[key]
    # 2) 環境変数が優先
    for key, (env_name, _title) in DB_REGISTRY.items():
        if env.get(env_name):
            db_ids[key] = env[env_name]
    return db_ids


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def load_settings(*, dry_run: bool | None = None, env: dict[str, str] | None = None) -> Settings:
    """環境から設定を構築する。dry_run 引数は環境変数 DRY_RUN より優先。"""
    env = dict(os.environ) if env is None else env
    resolved_dry_run = _truthy(env.get("DRY_RUN")) if dry_run is None else dry_run
    return Settings(
        notion_token=env.get("NOTION_TOKEN") or None,
        notion_parent_page_id=env.get("NOTION_PARENT_PAGE_ID", DEFAULT_PARENT_PAGE_ID),
        edinet_api_key=env.get("EDINET_API_KEY") or None,
        notion_rps=float(env.get("NOTION_RPS", DEFAULT_NOTION_RPS)),
        raw_data_dir=Path(env.get("RAW_DATA_DIR", "data/raw")),
        dry_run=resolved_dry_run,
        stooq_enabled=_truthy(env.get("STOOQ_ENABLED")),
        db_ids=_load_db_ids(env),
        local_store=_load_local_store(env),
    )
