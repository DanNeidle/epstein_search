# © Dan Neidle and Tax Policy Associates 2026
import os
import re

MAX_LOOPS = 50  # Safety limit: max number of autonomous tool calls per user request
MIN_LOOPS = 5
MODEL_NAME = "gemini-3-pro-preview"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(APP_DIR, ".."))
DATA_DIR = os.path.join(ROOT_DIR, "data")
ASSETS_DIR = os.path.join(ROOT_DIR, "assets")
STATIC_DIR = os.path.join(APP_DIR, "static")
USERS_DB_PATH = os.path.join(APP_DIR, "users.db")
CSS_PATH = os.path.join(APP_DIR, "gemini_chat.css")
SYSTEM_PROMPT_PATH = os.path.join(APP_DIR, "system_prompt.md")
LOGO_PATH = os.path.join(ROOT_DIR, "logo_full_white_on_blue.jpg")
USER_AVATAR_PATH = os.path.join(STATIC_DIR, "avatar_user.svg")
ASSISTANT_AVATAR_PATH = os.path.join(STATIC_DIR, "avatar_assistant.svg")
PBKDF2_ITERATIONS = 200_000
SESSION_TOKEN_BYTES = 32
AUTH_COOKIE_NAME = "ep_auth"

DOC_URL_RE = re.compile(r"https?://[^)\s]+/f/([0-9a-f]{32})")
SOURCE_DOC_ID_RE = re.compile(r"^[0-9a-f]{32}$")
DOC_RESULT_SUMMARY_RE = re.compile(
    r"^(?P<name>.+?) \((?P<pages>\d+|\?) pages(?:, [\d,]+ bytes)?\) (?P<link>https?://\S+/f/[0-9a-f]{32})(?:\s+\[NEAR-DUPLICATE\])?$"
)
BATES_EXACT_RE = re.compile(r"^EFTA\d{8}$")
BATES_RE = re.compile(r"\bEFTA\d{8}\b")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
INTENT_BLOCK_RE = re.compile(r"^<intent>(?P<body>[\s\S]+)</intent>$")
MAX_INTENT_BODY_CHARS = 220
UNVERIFIED_DRAFT_MARKER = "<!--TPA_UNVERIFIED_DRAFT-->"
MAX_QUOTE_VALIDATION_FAILURES = 3

ES_URL = os.environ.get("EP_ES_URL", "http://localhost:9200")
ES_INDEX = os.environ.get("EP_ES_INDEX", "sist2")
SIST2_URL = os.environ.get("EP_SIST2_URL", "http://localhost:1997")
DEFAULT_HIGHLIGHT_FRAGMENT_SIZE = 300
DEFAULT_HIGHLIGHT_FRAGMENTS = 3
DEFAULT_LIMIT = 100  # Default to 100 hits for broader initial sweeps
LIST_PAGE_SIZE = 1000
MAX_TOOL_OUTPUT_CHARS = 2_200_000
VERIFICATION_MAX_DOC_CHARS = 80_000
# Was 260_000. 2M chars is safe for the total context.
VERIFICATION_MAX_TOTAL_SOURCE_CHARS = 2_000_000
ES_SEARCH_LIMIT_MIN = 1
ES_SEARCH_LIMIT_MAX = 500
DEEP_SWEEP_RESULT_THRESHOLD = 10
DEEP_SWEEP_COUNT_THRESHOLD = 20
DEEP_SWEEP_LIMIT_MIN = 100
DEEP_SWEEP_TARGET_FRACTION = 0.30
DEEP_SWEEP_MIN_BATCH_DOCS = 50
DEEP_SWEEP_MAX_BATCH_DOCS = 200
DEEP_SWEEP_SMALL_BATCH_RETRIES = 2
ES_SEARCH_FRAGMENT_SIZE_MIN = 50
ES_SEARCH_FRAGMENT_SIZE_MAX = 2000
ES_SEARCH_FRAGMENTS_MIN = 1
ES_SEARCH_FRAGMENTS_MAX = 10
ES_READ_MAX_CHARS_MIN = 200
ES_READ_MAX_CHARS_MAX = 200_000
ES_READ_BATCH_MAX_TOTAL_CHARS_DEFAULT = 2_000_000

INPUT_RATE_LE_200K = 2.00
INPUT_RATE_GT_200K = 4.00
OUTPUT_RATE_LE_200K = 12.00
OUTPUT_RATE_GT_200K = 18.00
CACHE_RATE_LE_200K = 0.20
CACHE_RATE_GT_200K = 0.40
COST_PROMPT_LARGE_THRESHOLD = 200_000
MAX_TITLE_LEN = 64
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.pdf$")
MIN_FULL_DOC_READS = 3
