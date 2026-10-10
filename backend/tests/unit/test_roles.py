import pytest

from app.db.roles import (
    ROLE_API,
    ensure_login_sql,
    grant_connect_sql,
    quote_identifier,
    quote_literal,
    revoke_connect_sql,
)


def test_quote_literal_escapes_quotes() -> None:
    assert quote_literal("plain") == "'plain'"
    assert quote_literal("o'brien") == "'o''brien'"


def test_ensure_login_sql_preserves_existing_password() -> None:
    statement = ensure_login_sql(ROLE_API, "o'brien")

    assert "CREATE ROLE cc0_api" in statement
    assert "ALTER ROLE cc0_api" in statement
    assert "PASSWORD" not in statement.split("ELSE", 1)[1]
    assert "PASSWORD 'o''brien'" in statement
    assert "NOSUPERUSER" in statement
    assert "NOCREATEROLE" in statement


def test_connect_statements_use_the_current_database_name() -> None:
    assert grant_connect_sql("cc0", ROLE_API) == "GRANT CONNECT ON DATABASE cc0 TO cc0_api"
    assert revoke_connect_sql("cc0", ROLE_API) == "REVOKE CONNECT ON DATABASE cc0 FROM cc0_api"


def test_identifiers_reject_sql() -> None:
    with pytest.raises(ValueError):
        quote_identifier("cc0; drop role cc0_api")
    with pytest.raises(ValueError):
        grant_connect_sql("cc0", "not_a_role")
