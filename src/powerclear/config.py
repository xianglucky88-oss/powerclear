
import os
from types import SimpleNamespace

ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]
# Default offline database; overridable so runs/tests can target an
# isolated file without touching historical data.
DB_PATH = ROOT / "powerclear.db"
_env_db = os.environ.get("POWERCLEAR_DB")
if _env_db:
    DB_PATH = __import__("pathlib").Path(_env_db)

SPEC = SimpleNamespace(
    slug="powerclear",
    fmt="json",
    engine="crawl",
    endpoint="items",
    columns=["id", "name", "value", "status", "ts", "node"],
    page_size=4,
    int_fields=["value"],
    ts_field="ts",
)
