import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

/**
 * 公開面の bind 構成を設定ファイルで固定する。
 *
 * personal-only の R2 を公開 Worker に bind しないことが第0層の防御であり、
 * 「うっかり足す」を型やコードレビューだけに頼らない。
 */
function config(path: string): Record<string, unknown> {
  const text = readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
  // jsonc のコメントを落としてから読む
  return JSON.parse(text.replace(/^\s*\/\/.*$/gm, ""));
}

describe("Worker のバインディング", () => {
  it("公開面は personal-only のバケットを bind しない", () => {
    const c = config("wrangler.public.jsonc");
    const buckets = (c.r2_buckets as { bucket_name: string }[]).map((b) => b.bucket_name);
    expect(buckets).toEqual(["jp-stock-raw"]);
    expect(buckets).not.toContain("jp-stock-supply");
    expect(buckets).not.toContain("vwap-data");
  });

  it("公開面のバインディング名に SUPPLY / TS が無い", () => {
    const c = config("wrangler.public.jsonc");
    const names = (c.r2_buckets as { binding: string }[]).map((b) => b.binding);
    expect(names).not.toContain("SUPPLY");
    expect(names).not.toContain("TS");
  });

  it("内部面は需給バケットを bind する", () => {
    const c = config("wrangler.private.jsonc");
    const buckets = (c.r2_buckets as { bucket_name: string }[]).map((b) => b.bucket_name);
    expect(buckets).toContain("jp-stock-supply");
  });

  it("両面とも同じ D1 を読む（データを複製しない）", () => {
    const pub = config("wrangler.public.jsonc");
    const priv = config("wrangler.private.jsonc");
    const idOf = (c: Record<string, unknown>) =>
      (c.d1_databases as { database_id: string }[])[0]!.database_id;
    expect(idOf(pub)).toBe(idOf(priv));
  });

  it("surface が設定ファイルで宣言されている", () => {
    expect((config("wrangler.public.jsonc").vars as Record<string, string>).SURFACE)
      .toBe("public");
    expect((config("wrangler.private.jsonc").vars as Record<string, string>).SURFACE)
      .toBe("private");
  });
});
