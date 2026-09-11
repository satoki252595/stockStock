/** D1 / R2 の最小スタブ。実通信はしない。 */

export interface StubRow {
  [key: string]: unknown;
}

export function stubD1(handler: (sql: string, params: unknown[]) => StubRow[]) {
  return {
    prepare(sql: string) {
      const params: unknown[] = [];
      const stmt = {
        bind(...args: unknown[]) {
          params.push(...args);
          return stmt;
        },
        async all() {
          return { results: handler(sql, params) };
        },
        async first() {
          return handler(sql, params)[0] ?? null;
        },
      };
      return stmt;
    },
  } as unknown as D1Database;
}

export function stubR2(objects: Record<string, unknown>) {
  return {
    async get(key: string) {
      if (!(key in objects)) return null;
      const value = objects[key];
      return {
        async json() {
          return value;
        },
        body: new Blob([JSON.stringify(value)]).stream(),
        httpMetadata: { contentType: "application/json" },
      };
    },
  } as unknown as R2Bucket;
}
