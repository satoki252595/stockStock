/**
 * MCP サーバ（Streamable HTTP・ステートレス）。
 *
 * JSON-RPC 2.0 の `initialize` / `tools/list` / `tools/call` だけを実装する。
 * ステートレスにしているのは、セッションを持つと Worker のインスタンス間で
 * 状態を共有する必要が出る（Durable Object が要る）ため。読み取り専用の
 * データ参照ツールしか出さないのでセッションは不要。
 *
 * ツール名は `jp_*` 名前空間。kabuMCP の `edinet_*` とは分ける（両方を
 * クライアントに登録できるので統合しなくてよい）。
 */
import { envelope } from "./envelope";
import { isValidCode, parseLimit } from "./routes";
import type { PrivateEnv } from "./types";

interface JsonRpcRequest {
  jsonrpc: string;
  id?: string | number | null;
  method: string;
  params?: Record<string, unknown>;
}

export const TOOLS = [
  {
    name: "jp_supply_latest",
    description:
      "日本株の需給（日証金の貸借取引残高）の最新断面を返す。data_type は " +
      "jsf_zandaka（貸借残高）か jsf_shina（逆日歩）。personal-only のため私的利用限定。",
    inputSchema: {
      type: "object",
      properties: {
        data_type: { type: "string", enum: ["jsf_zandaka", "jsf_shina"] },
        limit: { type: "integer", minimum: 1, maximum: 500 },
      },
    },
  },
  {
    name: "jp_supply_series",
    description:
      "1銘柄の需給時系列を返す。融資残高・貸株残高・信用倍率・回転日数・逆日歩。" +
      "personal-only のため私的利用限定。",
    inputSchema: {
      type: "object",
      properties: {
        code: { type: "string", description: "4桁の銘柄コード" },
        series: { type: "string", enum: ["jsf_zandaka", "jsf_shina"] },
        from: { type: "string", description: "YYYY-MM-DD" },
        to: { type: "string", description: "YYYY-MM-DD" },
      },
      required: ["code"],
    },
  },
  {
    name: "jp_dataset_freshness",
    description: "各データセットの最新基準日・件数・格納先を返す。取り込みの鮮度確認用。",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "jp_xbrl_elements",
    description:
      "XBRL の勘定科目（element）の語彙表を前方一致で引く。どの科目が存在するかの確認用。",
    inputSchema: {
      type: "object",
      properties: {
        prefix: { type: "string" },
        limit: { type: "integer", minimum: 1, maximum: 500 },
      },
    },
  },
  {
    name: "jp_raw_file",
    description:
      "⑤原本ファイルのメタデータを SHA256 で引く。R2 のキー・サイズ・ライセンスを返す。" +
      "実体は REST の /v1/files/{sha256}/content から取得する。",
    inputSchema: {
      type: "object",
      properties: { sha256: { type: "string" } },
      required: ["sha256"],
    },
  },
] as const;

async function callTool(
  name: string,
  args: Record<string, unknown>,
  env: PrivateEnv,
): Promise<unknown> {
  switch (name) {
    case "jp_dataset_freshness": {
      const { results } = await env.DB.prepare(
        "SELECT dataset, store, location, latest_data_date, row_or_object_count," +
          " license_tag, updated_at FROM jss_dataset_freshness ORDER BY dataset",
      ).all();
      return envelope(results);
    }
    case "jp_supply_latest": {
      const limit = parseLimit(String(args.limit ?? ""), 100);
      const dataType = args.data_type ? String(args.data_type) : null;
      const stmt = dataType
        ? env.DB.prepare(
            "SELECT code, data_type, data_date, loan_bal, stock_bal, ratio, turn_days" +
              " FROM jss_supply_latest WHERE data_type = ? ORDER BY code LIMIT ?",
          ).bind(dataType, limit)
        : env.DB.prepare(
            "SELECT code, data_type, data_date, loan_bal, stock_bal, ratio, turn_days" +
              " FROM jss_supply_latest ORDER BY code, data_type LIMIT ?",
          ).bind(limit);
      const { results } = await stmt.all();
      return envelope(results, { sources: ["日証金"], licenses: ["personal-only"] });
    }
    case "jp_supply_series": {
      const code = String(args.code ?? "");
      if (!isValidCode(code)) throw new Error("銘柄コードは4桁");
      const object = await env.SUPPLY.get(`supply/${code}.json`);
      if (!object) throw new Error(`需給データが無い: ${code}`);
      const payload = (await object.json()) as Record<string, unknown>;
      const series = (payload.series ?? {}) as Record<string, Array<Record<string, unknown>>>;
      const wanted = args.series ? String(args.series) : null;
      const from = args.from ? String(args.from) : null;
      const to = args.to ? String(args.to) : null;
      const filtered: Record<string, unknown[]> = {};
      for (const [key, points] of Object.entries(series)) {
        if (wanted && key !== wanted) continue;
        filtered[key] = points.filter((p) => {
          const d = String(p.d ?? "");
          return (!from || d >= from) && (!to || d <= to);
        });
      }
      return envelope({ code, updated: payload.updated, series: filtered }, {
        sources: ["日証金"], licenses: ["personal-only"],
      });
    }
    case "jp_xbrl_elements": {
      const limit = parseLimit(String(args.limit ?? ""), 100);
      const prefix = args.prefix ? String(args.prefix) : null;
      const stmt = prefix
        ? env.DB.prepare(
            "SELECT element, namespace, doc_count, is_text_block FROM jss_xbrl_elements" +
              " WHERE element >= ?1 AND element < ?1 || CHAR(0x10FFFF) ORDER BY element LIMIT ?2",
          ).bind(prefix, limit)
        : env.DB.prepare(
            "SELECT element, namespace, doc_count, is_text_block FROM jss_xbrl_elements" +
              " ORDER BY element LIMIT ?",
          ).bind(limit);
      const { results } = await stmt.all();
      return envelope(results);
    }
    case "jp_raw_file": {
      const sha = String(args.sha256 ?? "");
      if (!/^[0-9a-f]{64}$/.test(sha)) throw new Error("sha256 は 64 桁の16進数");
      const row = await env.DB.prepare(
        "SELECT sha256, r2_bucket, r2_key, derived_key, source, datatype, scope," +
          " doc_id, code, data_date, ext, size_bytes, license_tag FROM jss_raw_files" +
          " WHERE sha256 = ?",
      ).bind(sha).first();
      if (!row) throw new Error("見つからない");
      return envelope(row);
    }
    default:
      throw new Error(`未知のツール: ${name}`);
  }
}

function rpcResult(id: unknown, result: unknown) {
  return { jsonrpc: "2.0", id: id ?? null, result };
}

function rpcError(id: unknown, code: number, message: string) {
  return { jsonrpc: "2.0", id: id ?? null, error: { code, message } };
}

export async function handleMcp(request: Request, env: PrivateEnv): Promise<Response> {
  let body: JsonRpcRequest;
  try {
    body = (await request.json()) as JsonRpcRequest;
  } catch {
    return Response.json(rpcError(null, -32700, "JSON として読めない"), { status: 400 });
  }
  const { id, method, params } = body;

  if (method === "initialize") {
    return Response.json(
      rpcResult(id, {
        protocolVersion: "2025-06-18",
        capabilities: { tools: {} },
        serverInfo: { name: "jp-stock-pipeline", version: "1.0.0" },
      }),
    );
  }
  if (method === "notifications/initialized") {
    return new Response(null, { status: 202 });
  }
  if (method === "tools/list") {
    return Response.json(rpcResult(id, { tools: TOOLS }));
  }
  if (method === "tools/call") {
    const name = String(params?.name ?? "");
    const args = (params?.arguments ?? {}) as Record<string, unknown>;
    try {
      const result = await callTool(name, args, env);
      return Response.json(
        rpcResult(id, {
          content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
        }),
      );
    } catch (exc) {
      return Response.json(
        rpcResult(id, {
          content: [{ type: "text", text: `エラー: ${(exc as Error).message}` }],
          isError: true,
        }),
      );
    }
  }
  return Response.json(rpcError(id, -32601, `未対応のメソッド: ${method}`), { status: 400 });
}
