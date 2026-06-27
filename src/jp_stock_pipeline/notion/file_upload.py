"""⑤ 原本ファイルDBへの原本+変換版アップロード (DESIGN.md §5.2, §8.1-4)。

CONTRACTS.md「⑤ 原本アップロードの契約」の実装:

    upload_raw_artifact(client, settings, artifact: RawArtifact) -> str  # ⑤の page_id

手順:
1. ⑤ を SHA256 プロパティで query → 既存ならその page_id を
   artifact.notion_page_id に設定して返す (重複スキップ §8.1-2)
2. File Upload API (notion-client 未対応のため client.raw_api() 経由):
   - 20MB 以下: mode=single_part → send 1回
   - 20MB 超: mode=multi_part (part_size 10MB) → part_number 毎に send → complete
   - send の Content-Type は multipart/form-data (requests の files= 引数経由)
   - 原本 + artifact.converted_paths をすべてアップロードする
3. ⑤ へ行作成: ファイル名(title=命名規則名 §5.2)/ファイル(file_upload 添付)/
   データ種別/対象銘柄コード/対象期間/取得URL/SHA256/サイズ/変換状態/
   ソース/ライセンスタグ/データ基準日/取得日時
4. 失敗時は RawUploadError を送出。呼び出し側 (ジョブ) はその取得単位の
   構造化データ書き込みを中止する (§8.1-4 原本必須の保証)

dry-run では client が操作を記録のみ行い合成ID ("dry-run-*") を返す前提で動作する。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

from ..config import Settings
from ..models import RawArtifact
from . import schema as S
from .client import NotionClient
from .upsert import (
    date_prop,
    files_prop,
    number_prop,
    select_prop,
    text_prop,
    title_prop,
    url_prop,
)

logger = logging.getLogger(__name__)

# Notion 有料プラン: 単純アップロード上限 20MB、超過はマルチパート (§0-1, §5.2)
SINGLE_PART_LIMIT = 20 * 1024 * 1024
# マルチパートの1パートサイズ (Notion仕様: 最終パート以外は 5〜20MB)
MULTIPART_CHUNK = 10 * 1024 * 1024


class RawUploadError(RuntimeError):
    """⑤ への原本アップロード失敗。

    呼び出し側 (ジョブ) は当該取得単位の構造化データ書き込みを中止すること
    (§8.1-4: 原本必須の保証)。
    """


def plan_upload(size_bytes: int) -> tuple[str, int]:
    """サイズからアップロードモードとパート数を決める (§5.2)。

    20MB 以下 → ("single_part", 1) / 20MB 超 → ("multi_part", ceil(size/10MB))
    """
    if size_bytes <= SINGLE_PART_LIMIT:
        return ("single_part", 1)
    return ("multi_part", math.ceil(size_bytes / MULTIPART_CHUNK))


def _created_upload_id(resp: dict, path: Path) -> str:
    upload_id = resp.get("id")
    if not upload_id:
        raise RawUploadError(f"File Upload 作成応答に id が無い: {path.name}: {resp}")
    return upload_id


def _upload_single(client: NotionClient, path: Path) -> str:
    created = client.raw_api(
        "POST", "file_uploads", json_body={"mode": "single_part", "filename": path.name}
    )
    upload_id = _created_upload_id(created, path)
    # bytes で渡す: ファイルハンドルだと 429/5xx リトライ時に消費済みハンドルの
    # 再送 = 空ボディ送信になるため (multipart/form-data, requests の files= 経由)
    content = path.read_bytes()
    client.raw_api(
        "POST", f"file_uploads/{upload_id}/send", files={"file": (path.name, content)}
    )
    return upload_id


def _upload_multipart(client: NotionClient, path: Path, n_parts: int) -> str:
    created = client.raw_api(
        "POST",
        "file_uploads",
        json_body={
            "mode": "multi_part",
            "filename": path.name,
            "number_of_parts": n_parts,
        },
    )
    upload_id = _created_upload_id(created, path)
    with path.open("rb") as fp:
        for part_number in range(1, n_parts + 1):
            chunk = fp.read(MULTIPART_CHUNK)
            client.raw_api(
                "POST",
                f"file_uploads/{upload_id}/send",
                data={"part_number": str(part_number)},
                files={"file": (path.name, chunk)},
            )
    client.raw_api("POST", f"file_uploads/{upload_id}/complete", json_body={})
    return upload_id


def upload_file(client: NotionClient, path: Path) -> str:
    """1ファイルを File Upload API でアップロードし file_upload id を返す。"""
    size = path.stat().st_size
    mode, n_parts = plan_upload(size)
    logger.info("アップロード %s (%d bytes, %s, %dパート)", path.name, size, mode, n_parts)
    if mode == "single_part":
        return _upload_single(client, path)
    return _upload_multipart(client, path, n_parts)


def _raw_row_properties(artifact: RawArtifact, uploads: list[tuple[str, str]]) -> dict:
    """⑤ の行プロパティ (§6.4)。共通プロパティは §6.3 の⑤向けサブセット。"""
    props = {
        S.RAW_PROP_FILENAME: title_prop(artifact.filename),
        S.RAW_PROP_FILES: files_prop(uploads),
        S.RAW_PROP_DATATYPE: text_prop(artifact.datatype),
        S.RAW_PROP_SCOPE: text_prop(artifact.scope),
        S.RAW_PROP_URL: url_prop(artifact.url),
        S.RAW_PROP_SHA256: text_prop(artifact.sha256),
        S.RAW_PROP_SIZE: number_prop(artifact.size_bytes),
        S.RAW_PROP_CONVERT_STATUS: select_prop(artifact.convert_status.value),
        # 共通プロパティ (⑤は ソース/ライセンスタグ/データ基準日/取得日時 のみ §6.3)
        S.PROP_SOURCE: select_prop(artifact.source.value),
        S.PROP_LICENSE_TAG: select_prop(artifact.license_tag.value),
        S.PROP_FETCHED_AT: date_prop(artifact.fetched_at),
    }
    if artifact.data_date is not None:
        props[S.PROP_DATA_DATE] = date_prop(artifact.data_date)
        props[S.RAW_PROP_PERIOD] = text_prop(artifact.data_date.isoformat())
    return props


def find_raw_page_by_sha256(
    client: NotionClient, settings: Settings, sha256: str
) -> str | None:
    """⑤ から SHA256 で既存行を探す (重複スキップ §8.1-2)。"""
    results = client.query_database(
        settings.db_id("raw_files"),
        filter={"property": S.RAW_PROP_SHA256, "rich_text": {"equals": sha256}},
        page_size=1,
        max_pages=1,
    )
    return results[0]["id"] if results else None


def upload_raw_artifact(
    client: NotionClient, settings: Settings, artifact: RawArtifact
) -> str:
    """原本+変換版を ⑤ へアップロードし行作成、page_id を返す (契約 §8.1-2〜4)。

    - SHA256 一致の既存行があれば再アップロードせずその page_id を返す (冪等)
    - 失敗時は RawUploadError (呼び出し側は構造化書き込みを中止すること)
    - 成功時は artifact.notion_page_id を設定する
    """
    try:
        # SHA256 重複クエリの失敗も契約例外に揃える (§8.1-4。NotionRequestError 等を
        # 漏らさず、呼び出し側は RawUploadError のみ握れば取得単位を degrade できる)
        existing = find_raw_page_by_sha256(client, settings, artifact.sha256)
        if existing:
            logger.info("⑤ 重複スキップ (SHA256=%s): %s", artifact.sha256[:12], existing)
            artifact.notion_page_id = existing
            return existing

        uploads: list[tuple[str, str]] = []
        for path in [artifact.local_path, *artifact.converted_paths]:
            uploads.append((upload_file(client, path), path.name))
        page = client.create_page(
            parent={"database_id": settings.db_id("raw_files")},
            properties=_raw_row_properties(artifact, uploads),
        )
        page_id = page.get("id")
        if not page_id:
            raise RawUploadError(f"⑤ 行作成応答に id が無い: {artifact.filename}: {page}")
    except RawUploadError:
        raise
    except Exception as exc:  # 重複クエリ/アップロード/行作成のあらゆる失敗を契約例外に揃える
        raise RawUploadError(f"⑤ への原本アップロード失敗: {artifact.filename}: {exc}") from exc

    artifact.notion_page_id = page_id
    logger.info("⑤ 行作成: %s -> %s (添付 %d ファイル)", artifact.filename, page_id, len(uploads))
    return page_id
