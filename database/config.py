import os
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv
from urllib.parse import quote_plus
# pyrefly: ignore [missing-import]
from sqlalchemy import create_engine




load_dotenv()

# Local Ollama API (for mistral_local)
MISTRAL_API_URL = os.getenv("MISTRAL_API_URL")

# API Keys
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL")

MISTRAL_LOCAL_URL = os.getenv("MISTRAL_LOCAL_URL")
MISTRAL_LOCAL_MODEL = os.getenv("MISTRAL_LOCAL_MODEL")

# Choose between: "gemini", "mistral_cloud", "mistral_local"
ACTIVE_LLM = os.getenv("ACTIVE_LLM")
MODEL_NAME = os.getenv("MODEL_NAME")

# ============ Folder Configuration ============
GRAPH_FOLDER = os.getenv("GRAPH_FOLDER", "graphs")
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "uploads")
TEMP_UPLOAD_FOLDER = os.getenv("TEMP_UPLOAD_FOLDER", "uploads")

# ============ MySQL Configuration ============
MYSQL_CONFIG = {
    "host": os.getenv("MYSQL_HOST"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "database": os.getenv("MYSQL_DATABASE")
}

# --- SQLAlchemy Engine ---
MYSQL_URI = (
    f"mysql+pymysql://{MYSQL_CONFIG['user']}:{quote_plus(MYSQL_CONFIG['password'] or '')}"
    f"@{MYSQL_CONFIG['host']}:{MYSQL_CONFIG['port']}/{MYSQL_CONFIG['database']}"
)

engine = create_engine(MYSQL_URI, pool_recycle=3600, pool_pre_ping=True)

# ============ Base URL ============
BASE_URL = os.getenv("BASE_URL")

# ============ Misc Settings ============
MAX_SAMPLE_VALUES = int(os.getenv("MAX_SAMPLE_VALUES", "100"))

# ============ ArangoDB Configuration ============
ARANGO_HOST = os.getenv("ARANGO_HOST")
ARANGO_USER = os.getenv("ARANGO_USER")
ARANGO_PASS = os.getenv("ARANGO_PASS")
ARANGO_DB = os.getenv("ARANGO_DB")

# ============ Email configuration ============
MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "True").lower() == "true"

MAIL_USERNAME = os.getenv("MAIL_USERNAME")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")

# ============ ChromaDB Configuration ============
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chroma_store"))
CHROMA_COLLECTION_NAME = os.getenv("CHROMA_COLLECTION_NAME", "dagent_knowledge")

