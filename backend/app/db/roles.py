"""Login roles created by the first migration. No catalog privileges yet."""

ROLE_MIGRATOR = "cc0_migrator"
ROLE_API = "cc0_api"
ROLE_WORKER = "cc0_worker"
ROLE_NAMES = (ROLE_MIGRATOR, ROLE_API, ROLE_WORKER)

_LOGIN_ATTRIBUTES = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def quote_identifier(value: str) -> str:
    if not value or not all(character.isalnum() or character == "_" for character in value):
        raise ValueError("identifier must contain only letters, digits, and underscores")
    if value[0].isdigit():
        raise ValueError("identifier must not start with a digit")
    return value


def ensure_login_sql(role: str, password: str) -> str:
    if role not in ROLE_NAMES:
        raise ValueError(f"unknown role: {role}")
    if not password or "$$" in password or "\x00" in password:
        raise ValueError("password is required and cannot contain $$ or a null")
    role_name = quote_identifier(role)
    password_literal = quote_literal(password)
    return f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role_name}') THEN
            CREATE ROLE {role_name} WITH {_LOGIN_ATTRIBUTES} PASSWORD {password_literal};
        ELSE
            ALTER ROLE {role_name} WITH {_LOGIN_ATTRIBUTES} PASSWORD {password_literal};
        END IF;
    END
    $$;
    """


def _known_role(role: str) -> str:
    if role not in ROLE_NAMES:
        raise ValueError(f"unknown role: {role}")
    return quote_identifier(role)


def grant_connect_sql(database: str, role: str) -> str:
    database_name = quote_identifier(database)
    role_name = _known_role(role)
    return f"GRANT CONNECT ON DATABASE {database_name} TO {role_name}"


def revoke_connect_sql(database: str, role: str) -> str:
    database_name = quote_identifier(database)
    role_name = _known_role(role)
    return f"REVOKE CONNECT ON DATABASE {database_name} FROM {role_name}"
