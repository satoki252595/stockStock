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

import contextlib
import logging
import math
import mimetypes
import tempfile
import zipfile
from pathlib import Path

from ..config import Settings
from ..models import RawArtifact
from . import schema as S
from .client import NotionClient
from .upsert import (
    KEY_QUERY_PAGE_SIZE,
    KEY_QUERY_SORTS,
    _oldest_page_ids,
    date_prop,
    files_prop,
    number_prop,
    oldest_page,
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

# Notion File Upload API が受け付ける拡張子（実 API プローブで 200 を確認した集合）。
# これ以外（.parquet/.xbrl 等）は create が 400「extension not supported」で弾かれるため
# .zip でラップしてからアップロードする（§5.2: 変換版を⑤に併置。Notion は .zip 対応）。
# 迷ったら narrow 側に倒す: 対応拡張子を誤って包んでも .zip は必ず通る（取得側で解凍）が、
# 非対応を素通しすると 400 で取得単位ごと中止になるため。
NOTION_UPLOAD_EXTENSIONS = frozenset(
    {"csv", "txt", "json", "zip", "tsv", "xml", "pdf", "xlsx", "htm", "html", "md", "yaml"}
)


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


def _fallback_content_type(path: Path) -> str | None:
    """create 応答に content_type が無い場合の保険（dry-run 等）。

    通常は Notion の create 応答が推定済み content_type を返すのでそれを正本にする。
    mimetypes は OS により結果が変わり（例: Linux runner で .csv→None）、その値を
    create と send の両方に使うと「content type … not supported」/「extension not
    supported」で 400 になる事故が起きたため、推定の正本にはしない。ここは応答が
    欠ける異常時のみのフォールバックで、None なら send は 2要素タプルに退避する。
    """
    ctype, _ = mimetypes.guess_type(path.name)
    return ctype


def _create_upload(client: NotionClient, path: Path, json_body: dict) -> tuple[str, str | None]:
    """File Upload を作成し (upload_id, content_type) を返す。

    content_type は Notion が filename から推定した create 応答の値を使う。send 時の
    パート content_type をこれに一致させないと 400 (validation_error) になる。
    create 側に content_type を渡さない（=Notion に推定させる）ことで、OS 依存の
    mimetypes に左右されず create↔send が必ず一致する。
    """
    created = client.raw_api("POST", "file_uploads", json_body=json_body)
    upload_id = _created_upload_id(created, path)
    ctype = created.get("content_type") or _fallback_content_type(path)
    return upload_id, ctype


def _file_part(name: str, content: bytes, ctype: str | None) -> tuple:
    """multipart/form-data の files= 用パート。

    ctype があれば 3要素タプルで明示する（create 応答の推定値と一致させる）。
    requests は content_type 省略時 text/plain 相当を送るため、ctype 既知なら必ず付ける。
    """
    return (name, content, ctype) if ctype else (name, content)


def _upload_single(client: NotionClient, path: Path) -> str:
    upload_id, ctype = _create_upload(
        client, path, {"mode": "single_part", "filename": path.name}
    )
    # bytes で渡す: ファイルハンドルだと 429/5xx リトライ時に消費済みハンドルの
    # 再送 = 空ボディ送信になるため (multipart/form-data, requests の files= 経由)。
    content = path.read_bytes()
    client.raw_api(
        "POST",
        f"file_uploads/{upload_id}/send",
        files={"file": _file_part(path.name, content, ctype)},
    )
    return upload_id


def _upload_multipart(client: NotionClient, path: Path, n_parts: int) -> str:
    upload_id, ctype = _create_upload(
        client,
        path,
        {"mode": "multi_part", "filename": path.name, "number_of_parts": n_parts},
    )
    with path.open("rb") as fp:
        for part_number in range(1, n_parts + 1):
            chunk = fp.read(MULTIPART_CHUNK)
            client.raw_api(
                "POST",
                f"file_uploads/{upload_id}/send",
                data={"part_number": str(part_number)},
                files={"file": _file_part(path.name, chunk, ctype)},
            )
    client.raw_api("POST", f"file_uploads/{upload_id}/complete", json_body={})
    return upload_id


@contextlib.contextmanager
def _zip_wrapped(path: Path):
    """Notion 非対応拡張子のファイルを `<name>.zip` に包んだ一時ファイルを yield する。

    zip 内のエントリ名は元ファイル名そのまま（取得側は解凍して原ファイルを得る）。
    一時ディレクトリに `<name>.zip` で作るのは、アップロード時の filename を
    元名 + .zip に保つため（_upload_* は path.name を Notion へ送る）。
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="notion_zip_"))
    zpath = tmpdir / f"{path.name}.zip"
    try:
        with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(path, arcname=path.name)
        yield zpath
    finally:
        with contextlib.suppress(OSError):
            zpath.unlink(missing_ok=True)
            tmpdir.rmdir()


def _upload_dispatch(client: NotionClient, path: Path) -> str:
    """サイズで single/multi を分岐して 1 ファイルを送り file_upload id を返す。"""
    size = path.stat().st_size
    mode, n_parts = plan_upload(size)
    logger.info("アップロード %s (%d bytes, %s, %dパート)", path.name, size, mode, n_parts)
    if mode == "single_part":
        return _upload_single(client, path)
    return _upload_multipart(client, path, n_parts)


def upload_file(client: NotionClient, path: Path) -> tuple[str, str]:
    """1ファイルをアップロードし (file_upload id, 添付ファイル名) を返す。

    Notion File Upload 非対応拡張子（.parquet 等）は .zip でラップして送るため、
    添付名が元名と変わりうる（`<name>` → `<name>.zip`）。呼び出し側は返った名前を
    ⑤ の files プロパティに使う。
    """
    ext = path.suffix.lstrip(".").lower()
    if ext and ext not in NOTION_UPLOAD_EXTENSIONS:
        logger.info("Notion 非対応拡張子 .%s を .zip ラップして UL: %s", ext, path.name)
        with _zip_wrapped(path) as zpath:
            return _upload_dispatch(client, zpath), zpath.name
    return _upload_dispatch(client, path), path.name


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
    """⑤ から SHA256 で既存行を探す (重複スキップ §8.1-2)。

    同じ SHA256 の行が複数あれば最古を返す (#13。③④の原本 relation の張り先を揃える)。
    ⑤ は作成直後の重複収束をしない: キーが内容のハッシュなので重複しても値は割れず、
    収束の確認クエリは原本 1 件ごとに増える（edinet_daily は書類 1 件で最大 2 件）ため。
    """
    results = client.query_database(
        settings.db_id("raw_files"),
        filter={"property": S.RAW_PROP_SHA256, "rich_text": {"equals": sha256}},
        sorts=KEY_QUERY_SORTS,
        page_size=KEY_QUERY_PAGE_SIZE,
        max_pages=1,
    )
    page = oldest_page(results)
    return page["id"] if page else None


def load_raw_page_map(
    client: NotionClient, settings: Settings, *, data_date
) -> dict[str, str]:
    """⑤ の {SHA256: page_id} を対象日 1 回のクエリで作る（L-21）。

    原本ごとに `find_raw_page_by_sha256`（1 req/件）を打っていたのを、
    対象日の ⑤ 1 スキャンにまとめる（④ の date-scoped map と同じ手法）。
    日付はデータ基準日（`PROP_DATA_DATE` の equals）で絞る。
    同じ SHA256 の重複行は最古勝ち（`find_raw_page_by_sha256` と同じ規則）。
    """
    pages = client.query_database(
        settings.db_id("raw_files"),
        filter={
            "property": S.PROP_DATA_DATE,
            "date": {"equals": data_date.isoformat()},
        },
    )

    def sha_of(page: dict) -> str:
        rich = page.get("properties", {}).get(S.RAW_PROP_SHA256, {}).get("rich_text", [])
        return rich[0].get("plain_text", "").strip() if rich else ""

    return _oldest_page_ids(pages, sha_of)


def upload_raw_artifact(
    client: NotionClient, settings: Settings, artifact: RawArtifact,
    *,
    sha_map: dict[str, str] | None = None,
    sha_map_date=None,
) -> str:
    """原本+変換版を ⑤ へアップロードし行作成、page_id を返す (契約 §8.1-2〜4)。

    - SHA256 一致の既存行があれば再アップロードせずその page_id を返す (冪等)
    - 失敗時は RawUploadError (呼び出し側は構造化書き込みを中止すること)
    - 成功時は artifact.notion_page_id を設定する

    `sha_map`（`load_raw_page_map` の結果）を渡すと、原本ごとの重複クエリを
    省く（L-21）。信用するのは `artifact.data_date == sha_map_date` の原本
    だけ（④ の date-scoped と同じ規則）。日付が違う・無い原本は従来どおり
    per-record 検索する。作った行は map へ足す（同一 run 内の再送に効く）。
    """
    try:
        trusted = (
            sha_map is not None
            and sha_map_date is not None
            and artifact.data_date is not None
            and artifact.data_date == sha_map_date
        )
        if trusted:
            existing = (sha_map or {}).get(artifact.sha256)
        else:
            # SHA256 重複クエリの失敗も契約例外に揃える (§8.1-4。NotionRequestError 等を
            # 漏らさず、呼び出し側は RawUploadError のみ握れば取得単位を degrade できる)
            existing = find_raw_page_by_sha256(client, settings, artifact.sha256)
        if existing:
            logger.info("⑤ 重複スキップ (SHA256=%s): %s", artifact.sha256[:12], existing)
            artifact.notion_page_id = existing
            return existing

        uploads: list[tuple[str, str]] = []
        for path in [artifact.local_path, *artifact.converted_paths]:
            upload_id, uploaded_name = upload_file(client, path)
            uploads.append((upload_id, uploaded_name))
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
    if trusted and sha_map is not None:
        sha_map[artifact.sha256] = page_id
    logger.info("⑤ 行作成: %s -> %s (添付 %d ファイル)", artifact.filename, page_id, len(uploads))
    return page_id
