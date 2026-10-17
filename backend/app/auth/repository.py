"""Insert-only mapping from verified authentication subjects to persistent users."""

from sqlalchemy import Engine, text

from app.auth.local import Identity


class UserRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def resolve(self, user: Identity) -> Identity:
        # A second statement observes a concurrent insert after ON CONFLICT waits.
        # No UPDATE privilege on users is needed.
        with self._engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO users (id, auth_subject) VALUES (:id, :subject)
                ON CONFLICT (auth_subject) DO NOTHING
            """),
                {"id": user.id, "subject": user.subject},
            )
            user_id = connection.execute(
                text("SELECT id FROM users WHERE auth_subject = :subject"),
                {"subject": user.subject},
            ).scalar_one()
        return Identity(user_id, user.subject)
