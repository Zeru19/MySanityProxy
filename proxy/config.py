import os
from dotenv import load_dotenv

load_dotenv()

UPSTREAM_URL = os.getenv("UPSTREAM_URL", "https://api.anthropic.com")
LISTEN_HOST = os.getenv("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8080"))
MODE = os.getenv("SANITY_MODE", "desensitize")  # "desensitize" | "transparent"
DB_PATH = os.getenv("DB_PATH", "sanity.db")
LOG_CAPACITY = 1000
