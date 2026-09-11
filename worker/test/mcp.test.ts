import { describe, expect, it } from "vitest";

import { TOOLS, handleMcp } from "../src/shared/mcp";
import type { PrivateEnv } from "../src/shared/types";
import { stubD1, stubR2 } from "./helpers";

const env: PrivateEnv = {
  DB: stubD1((sql) =>
    sql.includes("jss_supply_latest")
      ? [{ code: "7203", data_type: "jsf_zandaka", loan_bal: 100 }]
      : [{ dataset: "supply_jsf", latest_data_date: "2026-09-10" }],
  ),
  RAW: stubR2({}),
  SUPPLY: stubR2({
    "supply/7203.json": {
      code: "7203",
      updated: "2026-09-11",
      series: { jsf_zandaka: [{ d: "2026-09-10", ex: "東証", loan_bal: 100 }] },
    },
  }),
  SURFACE: "private",
  JSS_API_KEYS: "k",
};

function rpc(method: string, params?: Record<string, unknown>) {
  return new Request("https://x/mcp", {
    method: "POST",
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
}

describe("MCP", () => {
  it("initialize でプロトコル版と能力を返す", async () => {
    const res = await handleMcp(rpc("initialize"), env);
    const body = await res.json() as { result: { serverInfo: { name: string } } };
    expect(body.result.serverInfo.name).toBe("jp-stock-pipeline");
  });

  it("tools/list が jp_ 名前空間のツールを返す", async () => {
    const res = await handleMcp(rpc("tools/list"), env);
    const body = await res.json() as { result: { tools: { name: string }[] } };
    const names = body.result.tools.map((t) => t.name);
    expect(names).toEqual(TOOLS.map((t) => t.name));
    // kabuMCP の edinet_* と名前空間を分ける（両方登録できるようにするため）
    expect(names.every((n) => n.startsWith("jp_"))).toBe(true);
  });

  it("tools/call で需給の時系列を返す", async () => {
    const res = await handleMcp(
      rpc("tools/call", { name: "jp_supply_series", arguments: { code: "7203" } }), env,
    );
    const body = await res.json() as { result: { content: { text: string }[] } };
    expect(body.result.content[0]!.text).toContain("jsf_zandaka");
  });

  it("不正な銘柄コードは isError で返す（例外を外へ出さない）", async () => {
    const res = await handleMcp(
      rpc("tools/call", { name: "jp_supply_series", arguments: { code: "72031" } }), env,
    );
    const body = await res.json() as { result: { isError: boolean } };
    expect(body.result.isError).toBe(true);
  });

  it("未知のツールもエラー応答にする", async () => {
    const res = await handleMcp(
      rpc("tools/call", { name: "jp_nope", arguments: {} }), env,
    );
    const body = await res.json() as { result: { isError: boolean } };
    expect(body.result.isError).toBe(true);
  });

  it("未対応のメソッドは JSON-RPC エラー", async () => {
    const res = await handleMcp(rpc("resources/list"), env);
    const body = await res.json() as { error: { code: number } };
    expect(body.error.code).toBe(-32601);
  });

  it("personal-only のツールは licenses に印を持つ", async () => {
    const res = await handleMcp(
      rpc("tools/call", { name: "jp_supply_latest", arguments: {} }), env,
    );
    const body = await res.json() as { result: { content: { text: string }[] } };
    expect(body.result.content[0]!.text).toContain("personal-only");
  });
});
