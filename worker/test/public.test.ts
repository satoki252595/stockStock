import { describe, expect, it } from "vitest";

import app from "../src/public";
import type { PublicEnv } from "../src/shared/types";
import { stubD1, stubR2 } from "./helpers";

function env(rows: Record<string, unknown>[] = []): PublicEnv {
  return {
    DB: stubD1(() => rows),
    RAW: stubR2({ "raw/x": { ok: true } }),
    SURFACE: "public",
  };
}

describe("公開面", () => {
  it("書込メソッドを一切受けない", async () => {
    for (const method of ["POST", "PUT", "DELETE", "PATCH"]) {
      const res = await app.request("/v1/meta/licenses", { method }, env());
      expect(res.status).toBe(405);
    }
  });

  it("personal-only の原本はメタデータすら返さない", async () => {
    // メタだけでも日証金 CSV や JPX PDF の所在が漏れるため 403 にする。
    const res = await app.request(
      `/v1/files/${"a".repeat(64)}`,
      {},
      env([{ sha256: "a".repeat(64), license_tag: "personal-only", source: "日証金" }]),
    );
    expect(res.status).toBe(403);
  });

  it("factual-cite の原本も全量は返さない", async () => {
    const res = await app.request(
      `/v1/files/${"b".repeat(64)}`,
      {},
      env([{ sha256: "b".repeat(64), license_tag: "factual-cite", source: "TDnet" }]),
    );
    expect(res.status).toBe(403);
  });

  it("commercial-ok の原本は返す", async () => {
    const res = await app.request(
      `/v1/files/${"c".repeat(64)}`,
      {},
      env([{ sha256: "c".repeat(64), license_tag: "commercial-ok", source: "EDINET" }]),
    );
    expect(res.status).toBe(200);
    const body = await res.json() as { meta: { attribution: string[] } };
    expect(body.meta.attribution[0]).toContain("EDINET");
  });

  it("鮮度から personal-only の行を落とす", async () => {
    const res = await app.request("/v1/meta/freshness", {}, env([
      { dataset: "d1_core_stock_financials", license_tag: "personal-only" },
      { dataset: "ir_disclosures", license_tag: "factual-cite" },
    ]));
    const body = await res.json() as { data: { dataset: string }[] };
    expect(body.data.map((r) => r.dataset)).toEqual(["ir_disclosures"]);
  });

  it("不正な sha256 を 400 で弾く", async () => {
    const res = await app.request("/v1/files/nope", {}, env());
    expect(res.status).toBe(400);
  });

  it("需給のパスは公開面に存在しない", async () => {
    const res = await app.request("/v1/supply/7203", {}, env());
    expect(res.status).toBe(404);
  });
});
