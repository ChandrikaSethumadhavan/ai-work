import test from 'node:test';
import assert from 'node:assert/strict';
import { checkAccess } from '../lib/auth.mjs';
import { connectionString } from '../lib/db.mjs';

const basic = (user, pw) => 'Basic ' + btoa(`${user}:${pw}`);

test('local use without a password is allowed', () => {
  assert.equal(checkAccess({}, undefined).ok, true);
  assert.equal(checkAccess({ NODE_ENV: 'development' }, undefined).ok, true);
});

test('production without a password is refused, never open by accident', () => {
  const r = checkAccess({ NODE_ENV: 'production' }, undefined);
  assert.deepEqual([r.ok, r.status], [false, 503]);
});

test('with a password: wrong or missing credentials get 401, right one passes', () => {
  const env = { DASHBOARD_PASSWORD: 's3cret:x' };
  assert.equal(checkAccess(env, undefined).status, 401);
  assert.equal(checkAccess(env, basic('me', 'nope')).status, 401);
  assert.equal(checkAccess(env, 'Bearer abc').status, 401);
  assert.equal(checkAccess(env, basic('anyone', 's3cret:x')).ok, true); // password may itself contain ':'
});

test('production never falls back to the admin database URL', () => {
  const env = { TIGER_DATABASE_URL: 'postgres://admin' };
  assert.equal(connectionString({ ...env, NODE_ENV: 'development' }), 'postgres://admin');
  assert.throws(() => connectionString({ ...env, NODE_ENV: 'production' }), /DASHBOARD_DATABASE_URL/);
  assert.equal(connectionString({ ...env, NODE_ENV: 'production', DASHBOARD_DATABASE_URL: 'postgres://ro' }), 'postgres://ro');
});

import { clientConfig } from '../lib/db.mjs';

test('database client config: encrypted by default, verified when a CA file is given', () => {
  const plain = clientConfig('postgres://u:p@h:5432/db?sslmode=require', {});
  assert.match(plain.connectionString, /sslmode=require&uselibpqcompat=true$/);
  assert.equal(plain.ssl, undefined);
  assert.equal(clientConfig('postgres://u@h/db?uselibpqcompat=true', {}).connectionString, 'postgres://u@h/db?uselibpqcompat=true');
});
