/**
 * jss-api-private — 内部 REST + MCP。personal-only を含む全量。
 *
 * APIキー必須。**JSS_API_KEYS が未設定なら全リクエストを拒否する fail-closed**。
 * 未設定で素通しにすると、personal-only のデータ（日証金は規約で第三者提供を
 * 明文禁止）が無認証で出てしまう。
 */
import { Hono } from "hono";

import { errorBody } from "./shared/envelope";
import { handleMcp } from "./shared/mcp";
import { mountCommon, mountPrivate } from "./shared/routes";
import type { AnyEnv, PrivateEnv } from "./shared/types";

const app = new Hono<{ Bindings: PrivateEnv }>();

/** タイミング攻撃を避けるため長さと内容を定数時間で比べる。 */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

export function isAuthorized(env: PrivateEnv, header: string | undefined): boolean {
  const configured = (env.JSS_API_KEYS ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  if (configured.length === 0) return false; // fail-closed
  const presented = (header ?? "").trim();
  if (!presented) return false;
  return configured.some((key) => timingSafeEqual(key, presented));
}

app.use("*", async (c, next) => {
  if (c.req.path === "/health") return next();
  const configured = (c.env.JSS_API_KEYS ?? "").trim();
  if (!configured) {
    // 鍵が入っていない＝まだ運用開始していない。素通しにはしない。
    return c.json(errorBody("APIキーが未設定のため停止中", "not_configured"), 503);
  }
  if (!isAuthorized(c.env, c.req.header("X-API-Key"))) {
    return c.json(errorBody("APIキーが不正", "unauthorized"), 401);
  }
  c.header("X-Surface", "private");
  await next();
});

app.post("/mcp", (c) => handleMcp(c.req.raw, c.env));

mountPrivate(app);
mountCommon(app as unknown as Hono<{ Bindings: AnyEnv }>);

export default app;
