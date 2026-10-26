import fs from 'node:fs';
import pg from 'pg';

let pool;

// Production must use the read-only role. The admin URL is accepted only for local development.
export function connectionString(env = process.env) {
  if (env.DASHBOARD_DATABASE_URL) return env.DASHBOARD_DATABASE_URL;
  if (env.NODE_ENV !== 'production' && env.TIGER_DATABASE_URL) return env.TIGER_DATABASE_URL;
  throw new Error('DASHBOARD_DATABASE_URL is not set');
}

// Default: encrypted, certificate not verified (libpq `require` semantics, same as the Python side).
// For production set DASHBOARD_DB_CA_PATH to Tiger's CA certificate to also verify the server.
export function clientConfig(url, env = process.env) {
  if (env.DASHBOARD_DB_CA_PATH) return { connectionString: url, ssl: { ca: fs.readFileSync(env.DASHBOARD_DB_CA_PATH, 'utf8') } };
  const compat = url.includes('uselibpqcompat') ? url : url + (url.includes('?') ? '&' : '?') + 'uselibpqcompat=true';
  return { connectionString: compat };
}

export function query(sql, params = []) {
  pool ??= new pg.Pool({ ...clientConfig(connectionString()), max: 3 });
  return pool.query(sql, params).then((r) => r.rows);
}
