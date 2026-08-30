from app.config import Settings


def test_neon_database_url_is_normalized_for_asyncpg():
    settings = Settings(
        database_url=(
            "postgresql://sam:secret@example.neon.tech/neondb"
            "?sslmode=require&channel_binding=require"
        )
    )
    assert settings.database_url == (
        "postgresql+asyncpg://sam:secret@example.neon.tech/neondb?ssl=require"
    )
