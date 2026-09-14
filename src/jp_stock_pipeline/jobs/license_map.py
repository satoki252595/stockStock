"""license_map: 列単位ライセンス地図と参照表の**実行主体**。

## なぜこのジョブが必要だったか

`cloud_store/schema.py` の `seed_reference_tables()` / `apply_schema()` は
定義と `__all__` とテストはあるのに、**`jobs/*.py` からの呼び出しが 0 件**
だった（`grep -rn "apply_schema\\|seed_reference" src scripts deploy tests`）。
本番 `jss_column_license` の 7 行と `jss_index_symbols` の 6 行は、この経路の
外で一度だけ手で投入されたものである。

結果として次が成立していた:

- 宣言（コード）を直しても実表は変わらない。`sector33` のタグ誤りは
  コードとテストを直しただけでは本番に届かない
- 逆に実表を手で直しても宣言は変わらない。どちらが正か決める手段が無い
- `docs/TARGET-ARCHITECTURE.md` §8.1 が言う「2 つの真実が乖離しても誰も
  検知しない」状態そのもの

このジョブが**唯一の投入口**になる。`seed_reference_tables` を他から呼ばない
（投入口が複数あると、どれが最後に走ったかで実表の内容が変わる）。

## upsert は孤児宣言を消さない

`seed_reference_tables` の upsert は `conflict=(table_name, column_name)` で
DELETE を伴わない。**宣言から外した列の行は実表に残り続ける。** 残るのが
personal-only なら害は無いが、`commercial-ok` の孤児が残ると「公開してよい」と
宣言したまま誰も管理していない列ができる。投入のあとに実表を読み直して
突き合わせ、孤児は削除する。

`jss_index_symbols` の孤児は**削除しない**。`r2_key` が R2 の実オブジェクトを
指しており、行を消すと参照されないオブジェクトが残る（`r2.py` に削除 API が
無いので回収もできない）。報告だけにして、消すかどうかは R2 側の掃除と同じ
変更で決める。

## `apply_schema` を毎日は呼ばない

`apply_schema` は `CREATE TABLE/INDEX IF NOT EXISTS` を 20 文発行する。本番には
11 表すべてが既に存在するので、日次で呼んでも 20 往復ぶんの実行時間を使って
必ず no-op になる。代わりに `sqlite_master` 1 文（**行を走査しない**）で存在を
確かめ、欠けていたら「`apply_schema` を手で流せ」と報告する。ブートストラップ
（表が無い環境）は 1 回きりの操作なので、日次 cron の仕事ではない。

## 地図の網羅性（行を走査しない形で見る）

本番 30 表 375 列のうち、行タグも列地図も無いのが 22 表 / 257 列だった
（2026-09-13 の `sqlite_master` 実測。当初の「374 / 256」は 1 列ぶん少ない）。
`cloud_store/governance.TABLE_LICENSE` が表区分を持ち、ここが本番
`sqlite_master` と突き合わせる。**列名も `sqlite_master.sql` から読む**（30 表に
`PRAGMA table_info` を投げると往復が 30 回になる）。

全表に `SUM(col IS NOT NULL)` を打って実際の充填を測る案は採らない。
`ir_disclosures`(37,641) と `yutai_benefits`(8,314) を含めて 1 実行あたり
**約 6 万行の走査**になり、設計書がまさにその規模の走査を「桁で下げる」対象と
して挙げているのと正面衝突する。

## writer の排他宣言も同じ扱いにする

`jss_writer_claims` も本番 4 行に対して**照合コードが両リポジトリに 0 行**
だった。投入と照合をここで行う。照合は warn → fail の 2 段リリースで、
**2026-09-13 から 2 段目**（食い違い・宣言漏れ・宣言外の claim でジョブを失敗
させる。1 段目の本番実行では warning 0 件だった）。claim を投入するのも
書込ジョブなので、**照合は必ず投入のあと**に行う。「claim が無ければ例外」を
投入より前に置くと claim 行の無い DB への最初の実行が必ず異常終了して
ブートストラップ不能になる（根拠と戻し方は `governance.CLAIM_MISMATCH_IS_FAILURE`）。

`jss_writer_claims` の孤児も **prune しない**。本番の既存 4 行がどの dataset を
指しているかは本レーンからは読めず、推測で消すと読めない情報を壊す。

## 走査行数（D1 は走査行課金）

1 回の実行で読むのは `sqlite_master` 2 文と、`jss_column_license` /
`jss_index_symbols` / `jss_writer_claims` の全行（宣言の件数と同じオーダー =
合計 39 行）だけ。**実データの表は 1 行も読まない。**

`sqlite_master` は「行を走査しない」と書きたくなるが、D1 の `rows_read` には
**カタログの走査も計上される**。2026-09-13 に本番で実測した値は
`JSS_TABLES_SQL` が 105 行 / `ALL_TABLES_SQL` が 126 行で、合計 231 行。
1 実行あたりの読みは 39 + 231 = **約 270 行**で、表の件数だけで決まりデータ量
では増えない（`ir_disclosures` が 10 倍になっても変わらない）。
"""

from __future__ import annotations

import logging

from ..cloud_store import governance as G
from ..cloud_store import schema as S
from ..cloud_store.d1 import D1Error, D1Store
from .runner import JobContext, build_parser, main_exit, run_job

logger = logging.getLogger(__name__)

JOB_NAME = "license_map"


class LicenseMapIncomplete(RuntimeError):
    """1 つでも照合できなかった（runner に STATUS_FAILURE を出させるため）。

    `runner._status` は failed>0 かつ processed>0 を「一部失敗」= **exit 0** に
    する。素直に書くと、4 つの検査のうち 1 つが落ちても他が成功していれば
    `ops_check.yml` が緑になり Issue が立たない。地図の乖離は「一部失敗」で
    済ませてよい種類のものではないので、最後に例外を投げて落とす
    （`jobs/freshness_probe.py` の `ProbeIncomplete` と同じ理由）。
    """


def _store(ctx: JobContext) -> D1Store | None:
    """正本 D1 を直接組む。

    `ctx.cloud` は `runner` が `if not settings.dry_run:` の中でしか作らないので、
    参照すると `--dry-run` が必ず即失敗する（`ops_check` / `freshness_probe` が
    踏んだのと同じ穴）。
    """
    settings = ctx.settings.cloud_store
    if not settings.d1_enabled():
        ctx.add_failure(
            "d1",
            "D1 が未設定 (CF_ACCOUNT_ID / CF_API_TOKEN / CF_D1_DATABASE_ID)。"
            " 地図を投入・照合できない",
        )
        return None
    return D1Store(settings, writer=JOB_NAME)


def _check_tables_exist(store: D1Store, ctx: JobContext) -> bool:
    """`jss_*` が本番にあるか（sqlite_master のみ・行は走査しない）。"""
    try:
        rows = store.query(S.JSS_TABLES_SQL)
    except D1Error as exc:
        ctx.add_failure("tables", f"sqlite_master を読めない: {exc}")
        return False
    observed = {str(r.get("name") or "") for r in rows}
    missing = sorted(S.declared_tables() - observed)
    if missing:
        ctx.add_failure(
            "tables",
            f"宣言した jss_ 表が本番に無い: {missing}"
            "（ブートストラップは日次ジョブの仕事ではない。"
            " `cloud_store.schema.apply_schema` を手で 1 回流すこと）",
        )
        return False
    return True


def _check_coverage(store: D1Store, ctx: JobContext) -> None:
    """本番の (表, 列) が地図に載っているかを見る。**行を 1 行も走査しない。**

    `sqlite_master` 1 文で全表の DDL を取り、列名は DDL から読む。30 表に
    `PRAGMA table_info` を投げる案は往復が 30 回になるので採らない（SQLite は
    `ALTER TABLE ADD COLUMN` で保存済みの CREATE TABLE 文を書き換えるので、
    ALTER で足した列も DDL に出る。`core_stocks` の P4a の 12 列で確認できる）。

    全表に `SUM(col IS NOT NULL)` を打って実際の充填を測る案は採らない。
    `ir_disclosures`(37,641) と `yutai_benefits`(8,314) を含めて **1 実行あたり
    約 6 万行の走査**になり、`docs/CF-CANONICAL-DESIGN.md` がまさにその規模の
    走査を「桁で下げる」対象として挙げているのと正面衝突する。網羅性は
    「地図に (表, 列) が載っているか」で見れば足り、中身を見る必要が無い。
    """
    try:
        rows = store.query(G.ALL_TABLES_SQL)
    except D1Error as exc:
        ctx.add_failure("coverage", f"sqlite_master から表一覧を読めない: {exc}")
        return
    observed = {str(r.get("name") or ""): r.get("sql") for r in rows}
    report = G.coverage(observed)
    logger.info(
        "地図の網羅性: 本番 %d 表 / %d 列（宣言 %d 表・列地図 %d 行）",
        report.tables, report.columns, len(G.TABLE_LICENSE), len(S.column_license_rows()),
    )
    for warning in report.warnings:
        logger.warning("%s", warning)
    if report.failures:
        for failure in report.failures:
            ctx.add_failure("coverage", failure)
        return
    ctx.add_success()


def _sync_writer_claims(store: D1Store, ctx: JobContext) -> None:
    """writer の排他宣言を投入して照合する（warn → fail の 2 段リリースの 2 段目）。

    本番の既存 4 行はすべて `column_group='all'` / `writer='stockStock'` で、
    照合コードは**両リポジトリに 0 行**だった。しかも両方が書く `core_stocks` は
    誰の所有でもなかった。

    **順序が仕様: 読み → 差分があれば投入 → 読み直し → 照合。** claim を投入する
    のも書込ジョブなので、照合を投入より前に置くと claim 行の無い DB に対する
    最初の実行が必ず異常終了しブートストラップ不能になる。この順序なら、2 段目
    でも初回は投入した行を読み直して一致し成功する。投入のあとでも残る差分
    （upsert が届いていない・宣言外の claim がある）だけが失敗になる。

    差分が無ければ upsert を打たない（L-17）。毎日変わらない宣言を毎日
    書き直すのは D1 の書き込み課金と `updated_at` の無駄な更新になる。
    """
    try:
        rows = store.query(G.WRITER_CLAIMS_SQL)
        if G.writer_claims_need_seed(rows):
            store.upsert(
                "jss_writer_claims",
                ["dataset", "column_group", "writer", "updated_at"],
                G.writer_claim_rows(),
                conflict=["dataset", "column_group"],
            )
            rows = store.query(G.WRITER_CLAIMS_SQL)
    except D1Error as exc:
        ctx.add_failure("jss_writer_claims", f"claim を投入・照合できない: {exc}")
        return
    failures, warnings = G.writer_claim_problems(rows)
    for warning in warnings:
        logger.warning("writer claim: %s", warning)
    if failures:
        for failure in failures:
            ctx.add_failure("jss_writer_claims", failure)
        return
    logger.info(
        "writer claim: 宣言 %d 件を投入（照合は %s）",
        len(G.WRITER_CLAIMS),
        "一致。食い違い・宣言漏れは失敗にする（2 段リリースの2段目）"
        if G.CLAIM_MISMATCH_IS_FAILURE
        else "warning のみ（2 段リリースの1段目に戻している）",
    )
    ctx.add_success()


def _describe(diff: S.ReferenceDiff, table: str) -> list[str]:
    problems: list[str] = []
    if diff.missing:
        problems.append(f"{table}: 宣言にあって実表に無い {list(diff.missing)}")
    for key, got, want in diff.mismatched:
        problems.append(f"{table}: {key} のタグが実表 {got!r} / 宣言 {want!r} で食い違う")
    return problems


def _sync_column_license(store: D1Store, ctx: JobContext) -> None:
    """読み → 差分があれば投入 → 読み直し → 孤児を削除 → 残る差分を失敗にする。

    以前は `execute` が無条件で全表を seed してから読んでいた。宣言が
    変わらない日の書き込みは無駄なので、差分があるときだけ投入する（L-17）。
    """
    try:
        rows = store.query(S.COLUMN_LICENSE_SQL)
    except D1Error as exc:
        ctx.add_failure("jss_column_license", f"実表を読めない: {exc}")
        return

    diff = S.column_license_diff(rows)
    if diff.missing or diff.mismatched:
        try:
            S.seed_column_license(store)
            rows = store.query(S.COLUMN_LICENSE_SQL)
        except D1Error as exc:
            ctx.add_failure("jss_column_license", f"宣言を投入できない: {exc}")
            return
        diff = S.column_license_diff(rows)
    for orphan in diff.orphan:
        table_name, column_name = orphan
        try:
            store.query(S.COLUMN_LICENSE_DELETE_SQL, [table_name, column_name])
        except D1Error as exc:
            ctx.add_failure("jss_column_license", f"孤児宣言 {orphan} を消せない: {exc}")
            return
        # 消したことを黙らせない。「公開してよい」と宣言していた列が
        # 地図から外れた事実はレビューできる形で残す必要がある。
        logger.warning(
            "孤児宣言を削除: %s.%s（宣言から外れた列。upsert では消えないので明示削除）",
            table_name, column_name,
        )

    problems = _describe(diff, "jss_column_license")
    if problems:
        # 投入直後に食い違う = upsert が届いていない。黙って次回に期待しない。
        for p in problems:
            ctx.add_failure("jss_column_license", p)
        return
    logger.info(
        "列ライセンス地図: 宣言 %d 行と一致（孤児 %d 行を削除）",
        len(S.column_license_rows()), len(diff.orphan),
    )
    ctx.add_success()


def _sync_index_symbols(store: D1Store, ctx: JobContext) -> None:
    """指数シンボルの照合。孤児は**削除せず報告する**（R2 オブジェクトが残る）。

    欠損・食い違いがあるときだけ投入する（L-17）。以前の `_check_*` は
    投入を `execute` の一括 seed に頼っていたが、あれは無くなった。
    """
    try:
        rows = store.query(S.INDEX_SYMBOLS_SQL)
    except D1Error as exc:
        ctx.add_failure("jss_index_symbols", f"実表を読めない: {exc}")
        return
    diff = S.index_symbol_diff(rows)
    if diff.missing or diff.mismatched:
        try:
            S.seed_index_symbols(store)
            rows = store.query(S.INDEX_SYMBOLS_SQL)
        except D1Error as exc:
            ctx.add_failure("jss_index_symbols", f"宣言を投入できない: {exc}")
            return
        diff = S.index_symbol_diff(rows)
    problems = _describe(diff, "jss_index_symbols")
    if diff.orphan:
        # 失敗にはしない。R2 の掃除と同じ変更で消すべきもので、日次で鳴らすと
        # 「毎日鳴る通知」になり見なくなる（設計書 §7.6 の原則を自分にも適用）。
        logger.warning(
            "jss_index_symbols に宣言外の slug がある: %s"
            "（r2_key が実オブジェクトを指すので自動削除しない。R2 の掃除と同時に決める）",
            [k[0] for k in diff.orphan],
        )
    if problems:
        for p in problems:
            ctx.add_failure("jss_index_symbols", p)
        return
    ctx.add_success()


def execute(ctx: JobContext) -> None:
    store = _store(ctx)
    if store is None:
        return
    if not _check_tables_exist(store, ctx):
        return

    if ctx.settings.dry_run:
        # 1 バイトも書かない。代わりに投入しようとした内容を全部出す。
        for row in S.column_license_rows():
            logger.info("dry-run: jss_column_license へ投入しない行 %s", row)
        for row in S.index_symbol_rows():
            logger.info("dry-run: jss_index_symbols へ投入しない行 %s", row)
        for claim in G.WRITER_CLAIMS:
            logger.info("dry-run: jss_writer_claims へ投入しない行 %s", claim)
        # 網羅性の検査は読み取りだけなので dry-run でも回す
        # （本番へ触る前に「地図に無い表」を一覧できる唯一の手段）。
        _check_coverage(store, ctx)
        return

    # 一括 seed はしない。各表は「読む → 差分があれば投入 → 読み直し → 照合」
    # で、自分に必要なときだけ書く（L-17）。毎日変わらない宣言を毎日
    # 書き直すのは D1 の書き込み課金になる。
    _sync_column_license(store, ctx)
    _sync_index_symbols(store, ctx)
    _sync_writer_claims(store, ctx)
    _check_coverage(store, ctx)

    if ctx.failed:
        raise LicenseMapIncomplete(
            f"{ctx.failed} 件の照合に失敗した: {ctx.failed_codes}"
        )


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    parser = build_parser("列単位ライセンス地図と参照表を D1 へ投入し、実表と照合する")
    return run_job(JOB_NAME, execute, argv, parser=parser, env=env)


if __name__ == "__main__":
    main_exit(main())
