// Optional shared password (HTTP Basic, any username). Local use without a password is allowed,
// but a production deployment with no password is refused so the data is never exposed by accident.
export function checkAccess(env, authorization) {
  const password = env.DASHBOARD_PASSWORD;
  if (!password) {
    return env.NODE_ENV === 'production'
      ? { ok: false, status: 503, message: 'Set DASHBOARD_PASSWORD before deploying.' }
      : { ok: true };
  }
  const [scheme, encoded] = (authorization || '').split(' ');
  if (scheme === 'Basic' && encoded) {
    const given = atob(encoded).split(':').slice(1).join(':');
    if (given.length === password.length && [...given].every((c, i) => c === password[i])) return { ok: true };
  }
  return { ok: false, status: 401, message: 'Authentication required.', challenge: true };
}
