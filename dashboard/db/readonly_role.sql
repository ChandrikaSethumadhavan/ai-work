-- Run once in the Tiger Console SQL editor as the admin user. Replace CHANGE_ME with a strong password
-- (and URL-encode it when you put it in DASHBOARD_DATABASE_URL). Least privilege: rollups and review
-- tables only. No raw agent_events (payloads) and no code_chunks (source code).
CREATE ROLE dashboard_ro LOGIN PASSWORD 'CHANGE_ME';
GRANT CONNECT ON DATABASE tsdb TO dashboard_ro;
GRANT USAGE ON SCHEMA public TO dashboard_ro;
GRANT SELECT ON prs, reviews, hitl_items, agent_health_1m, pr_cost_hourly TO dashboard_ro;
