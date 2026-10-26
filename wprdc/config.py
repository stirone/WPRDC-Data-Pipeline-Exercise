"""Static configuration. Paths are overridable by environment variable for tests."""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = PROJECT_ROOT / "sql"
DATA_DIR = Path(os.environ.get("WPRDC_DATA_DIR", PROJECT_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
DB_PATH = Path(os.environ.get("WPRDC_DB_PATH", DATA_DIR / "wprdc.duckdb"))

CKAN_BASE_URL = "https://data.wprdc.org"
PACKAGE_ID = "real-estate-sales"
# "Property Sales Transactions": the full history. The package also carries
# one resource per sale year (2012-present) that duplicate subsets of it.
MAIN_RESOURCE_ID = "5bbe6c55-bce6-4edb-9d04-68edeb6bf7b1"

USER_AGENT = (
    "wprdc-pipeline-exercise/0.1 "
    "(+https://github.com/stirone/WPRDC-Data-Pipeline-Exercise)"
)
