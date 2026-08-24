import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
CORPUS_DIR = ROOT_DIR / "corpus"
MANIFEST_PATH = CORPUS_DIR / "manifest.jsonl"
UNIVERSE_PATH = CORPUS_DIR / "universe.csv"

CACHE_DIR = ROOT_DIR / "cache"
TEXT_CACHE_DIR = CACHE_DIR / "text"
FTS_DB_PATH = CACHE_DIR / "fulltext.sqlite3"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")

MAX_AGENT_TURNS = 12
