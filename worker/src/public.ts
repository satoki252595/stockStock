/**
 * jss-api-public — 公開 REST。
 *
 * 出せるのは commercial-ok の全量と factual-cite のメタデータ＋原典リンクのみ。
 * **SUPPLY / TS の R2 を bind していない**ので、コードにバグがあっても
 * personal-only のオブジェクトへ物理的に到達できない（第0層の防御）。
 */
import { Hono } from "hono";

import { errorBody } from "./shared/envelope";
import { mountCommon } from "./shared/routes";
import type { PublicEnv } from "./shared/types";

const app = new Hono<{ Bindings: PublicEnv }>();

app.use("*", async (c, next) => {
  await next();
  c.header("X-Surface", "public");
  // 公開面なので誰でも読める。書込面はこの Worker に存在しない。
  c.header("Access-Control-Allow-Origin", "*");
});

// 書込は公開面に一切置かない。GET と HEAD 以外は即拒否する。
app.use("*", async (c, next) => {
  if (c.req.method !== "GET" && c.req.method !== "HEAD") {
    return c.json(errorBody("公開面は読み取り専用", "method_not_allowed"), 405);
  }
  await next();
});

mountCommon(app as unknown as Hono<{ Bindings: import("./shared/types").AnyEnv }>);

export default app;
