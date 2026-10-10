-- Local login for the API readiness check. Alembic will own roles in the next slice.
-- Password matches the dummy value in .env.example. Do not use this outside local/test.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cc0_api') THEN
        CREATE ROLE cc0_api LOGIN PASSWORD 'cc0_api_local_password';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE cc0 TO cc0_api;
