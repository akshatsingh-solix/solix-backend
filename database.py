import os
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Fail fast when the database is unreachable: the driver's default is to wait 30s
# per request, which leaves the site and admin hanging rather than retrying.
# Pool size is tunable for busier instances (the driver default is 100).
client = AsyncIOMotorClient(
    os.environ['MONGO_URL'],
    serverSelectionTimeoutMS=int(os.environ.get('MONGO_SERVER_SELECTION_TIMEOUT_MS', '8000')),
    connectTimeoutMS=int(os.environ.get('MONGO_CONNECT_TIMEOUT_MS', '8000')),
    maxPoolSize=int(os.environ.get('MONGO_MAX_POOL_SIZE', '100')),
)
db = client[os.environ['DB_NAME']]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
