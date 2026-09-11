import { Hono } from "hono";

import { envelope, errorBody } from "./envelope";
import { isPublishableInFull, redactColumns } from "./license";
import type { AnyEnv, PrivateEnv } from "./types";

/** D1 の COUNT 等で 1 行だけ欲しいときの薄いヘルパ。 */
async function first<T>(stmt: D1PreparedStatement): Promise<T | null> {
  return (await stmt.first<T>()) ?? null;
}

const MAX_LIMIT = 500;

export function parseLimit(raw: string | undefined, fallback = 100): number {
  const n = Number.parseInt(raw ?? "", 10);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.min(n, MAX_LIMIT);
}

/** 4桁の銘柄コードだけ受ける。索引が効かない述語を外から作らせない。 */
export function isValidCode(code: string): boolean {
  return /^[0-9A-Z]{4}$/.test(code);
}

/**
 * 公開面・内部面で共通のルート。
 * surface に応じてライセンスフィルタの強さだけが変わる。
 */
export function mountCommon(app: Hono<{ Bindings: AnyEnv }>) {
  app.get("/v1/meta/licenses", (c) =>
    c.json(
      envelope({
        "commercial-ok": "商用・再配布可（出典記載が条件）",
        "factual-cite": "事実データの抽出利用可。原文は内部保管のみ",
        "personal-only": "私的利用限定。公開・商用組込は不可",
      }),
    ),
  );

  app.get("/v1/meta/freshness", async (c) => {
    const { results } = await c.env.DB.prepare(
      "SELECT dataset, store, location, latest_data_date, row_or_object_count," +
        " bytes, license_tag, updated_at FROM jss_dataset_freshness ORDER BY dataset",
    ).all<Record<string, unknown>>();
    const rows = c.env.SURFACE === "public"
      ? results.filter((r) => String(r.license_tag ?? "") !== "personal-only")
      : results;
    return c.json(envelope(rows, { licenses: rows.map((r) => String(r.license_tag ?? "")) }));
  });

  app.get("/v1/meta/jobs", async (c) => {
    const limit = parseLimit(c.req.query("limit"), 50);
    const jobName = c.req.query("job_name");
    const stmt = jobName
      ? c.env.DB.prepare(
          "SELECT job_name, status, processed, failed, run_url, duration_secs, finished_at" +
            " FROM jss_job_runs WHERE job_name = ? ORDER BY finished_at DESC LIMIT ?",
        ).bind(jobName, limit)
      : c.env.DB.prepare(
          "SELECT job_name, status, processed, failed, run_url, duration_secs, finished_at" +
            " FROM jss_job_runs ORDER BY finished_at DESC LIMIT ?",
        ).bind(limit);
    const { results } = await stmt.all<Record<string, unknown>>();
    return c.json(envelope(results));
  });

  app.get("/v1/xbrl/elements", async (c) => {
    const limit = parseLimit(c.req.query("limit"));
    const prefix = c.req.query("prefix");
    // 前方一致のみ。'%...%' は索引が効かず全走査になるので受けない。
    const stmt = prefix
      ? c.env.DB.prepare(
          "SELECT element, namespace, doc_count, is_text_block FROM jss_xbrl_elements" +
            " WHERE element >= ?1 AND element < ?1 || CHAR(0x10FFFF) ORDER BY element LIMIT ?2",
        ).bind(prefix, limit)
      : c.env.DB.prepare(
          "SELECT element, namespace, doc_count, is_text_block FROM jss_xbrl_elements" +
            " ORDER BY element LIMIT ?",
        ).bind(limit);
    const { results } = await stmt.all<Record<string, unknown>>();
    return c.json(envelope(results));
  });

  // ⑤原本のメタ。公開面は commercial-ok の行だけ返す。
  // メタだけでも日証金 CSV や JPX PDF の所在が漏れるため「メタは全行公開可」としない。
  app.get("/v1/files/:sha256", async (c) => {
    const sha = c.req.param("sha256");
    if (!/^[0-9a-f]{64}$/.test(sha)) {
      return c.json(errorBody("sha256 は 64 桁の16進数", "invalid_sha256"), 400);
    }
    const row = await first<Record<string, unknown>>(
      c.env.DB.prepare(
        "SELECT sha256, r2_bucket, r2_key, derived_key, derived_ext, source, datatype," +
          " scope, doc_id, code, data_date, ext, size_bytes, license_tag, convert_status" +
          " FROM jss_raw_files WHERE sha256 = ?",
      ).bind(sha),
    );
    if (!row) return c.json(errorBody("見つからない", "not_found"), 404);
    const tag = String(row.license_tag ?? "");
    if (c.env.SURFACE === "public" && !isPublishableInFull(tag)) {
      return c.json(errorBody("この原本は公開面では返せない", "restricted"), 403);
    }
    return c.json(envelope(row, { sources: [String(row.source ?? "")], licenses: [tag] }));
  });

  app.get("/v1/files/:sha256/content", async (c) => {
    const sha = c.req.param("sha256");
    if (!/^[0-9a-f]{64}$/.test(sha)) {
      return c.json(errorBody("sha256 は 64 桁の16進数", "invalid_sha256"), 400);
    }
    const row = await first<Record<string, unknown>>(
      c.env.DB.prepare(
        "SELECT r2_bucket, r2_key, derived_key, license_tag, ext FROM jss_raw_files WHERE sha256 = ?",
      ).bind(sha),
    );
    if (!row) return c.json(errorBody("見つからない", "not_found"), 404);
    const tag = String(row.license_tag ?? "");
    if (c.env.SURFACE === "public" && !isPublishableInFull(tag)) {
      return c.json(errorBody("この原本は公開面では返せない", "restricted"), 403);
    }
    const useDerived = c.req.query("derived") === "1";
    const key = String((useDerived ? row.derived_key : row.r2_key) ?? "");
    if (!key) return c.json(errorBody("その形式は存在しない", "not_found"), 404);
    // 原本は jp-stock-raw にしか無い。SUPPLY を参照しないので公開面でも安全。
    const object = await c.env.RAW.get(key);
    if (!object) return c.json(errorBody("R2 に実体が無い", "not_found"), 404);
    return new Response(object.body, {
      headers: {
        "Content-Type": object.httpMetadata?.contentType ?? "application/octet-stream",
        // 原本キーは SHA256 入りの immutable なので恒久キャッシュしてよい。
        "Cache-Control": "public, max-age=31536000, immutable",
      },
    });
  });

  app.get("/health", (c) => c.json({ ok: true, surface: c.env.SURFACE }));
  app.notFound((c) => c.json(errorBody("そのパスは無い", "not_found"), 404));
}

/** 内部面のみ。personal-only を含む。 */
export function mountPrivate(app: Hono<{ Bindings: PrivateEnv }>) {
  app.get("/v1/supply/latest", async (c) => {
    const limit = parseLimit(c.req.query("limit"));
    const dataType = c.req.query("data_type");
    const stmt = dataType
      ? c.env.DB.prepare(
          "SELECT code, data_type, data_date, loan_bal, stock_bal, ratio, turn_days," +
            " r2_key, license_tag FROM jss_supply_latest WHERE data_type = ?" +
            " ORDER BY code LIMIT ?",
        ).bind(dataType, limit)
      : c.env.DB.prepare(
          "SELECT code, data_type, data_date, loan_bal, stock_bal, ratio, turn_days," +
            " r2_key, license_tag FROM jss_supply_latest ORDER BY code, data_type LIMIT ?",
        ).bind(limit);
    const { results } = await stmt.all<Record<string, unknown>>();
    return c.json(envelope(results, { sources: ["日証金"], licenses: ["personal-only"] }));
  });

  app.get("/v1/supply/:code", async (c) => {
    const code = c.req.param("code");
    if (!isValidCode(code)) {
      return c.json(errorBody("銘柄コードは4桁", "invalid_code"), 400);
    }
    const object = await c.env.SUPPLY.get(`supply/${code}.json`);
    if (!object) return c.json(errorBody("見つからない", "not_found"), 404);
    const payload = (await object.json()) as Record<string, unknown>;
    const series = (payload.series ?? {}) as Record<string, Array<Record<string, unknown>>>;
    const wanted = c.req.query("series");
    const from = c.req.query("from");
    const to = c.req.query("to");
    const filtered: Record<string, Array<Record<string, unknown>>> = {};
    for (const [name, points] of Object.entries(series)) {
      if (wanted && name !== wanted) continue;
      filtered[name] = points.filter((p) => {
        const d = String(p.d ?? "");
        if (from && d < from) return false;
        if (to && d > to) return false;
        return true;
      });
    }
    return c.json(
      envelope({ code, updated: payload.updated, series: filtered }, {
        sources: ["日証金"],
        licenses: ["personal-only"],
      }),
    );
  });

  app.get("/v1/yutai/:code", async (c) => {
    const code = c.req.param("code");
    if (!isValidCode(code)) {
      return c.json(errorBody("銘柄コードは4桁", "invalid_code"), 400);
    }
    // みんかぶ掲載文の列は SELECT しない（規約上、取得も公開も不可）。
    const { results } = await c.env.DB.prepare(
      "SELECT b.genre_id, b.min_shares, b.record_month FROM yutai_benefits b" +
        " JOIN core_stocks s ON s.id = b.stock_id WHERE s.code = ? LIMIT 50",
    ).bind(code).all<Record<string, unknown>>();
    return c.json(envelope(results.map((r) => redactColumns("yutai_benefits", r))));
  });
}
