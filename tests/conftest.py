import os

# Set dummy environment variables for pytest collection/execution
os.environ.setdefault("DISCORD_TOKEN", "mock-discord-token")
os.environ.setdefault("RIOT_API_KEY", "mock-riot-api-key")
os.environ.setdefault("DAILY_REPORT_CHANNEL_ID", "123456789")
os.environ.setdefault("WEEKLY_REPORT_CHANNEL_ID", "123456789")
os.environ.setdefault("EVENTS_CHANNEL_ID", "123456789")
os.environ.setdefault("MATCH_RECAP_CHANNEL_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "postgresql://mock_user:mock_pass@localhost:5432/mock_db")
