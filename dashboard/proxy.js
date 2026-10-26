import { NextResponse } from 'next/server';
import { checkAccess } from './lib/auth.mjs';

export function proxy(request) {
  if (request.nextUrl.pathname === '/api/health') return NextResponse.next();
  const r = checkAccess(process.env, request.headers.get('authorization'));
  if (r.ok) return NextResponse.next();
  return new NextResponse(r.message, {
    status: r.status,
    headers: r.challenge ? { 'WWW-Authenticate': 'Basic realm="dashboard"' } : {},
  });
}
