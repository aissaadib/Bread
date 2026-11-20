"""Local media analysis components for Bread."""

from pathlib import Path
import json

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
DEFAULT_CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
ANALYSIS_VERSION = DEFAULT_CONFIG["analysis_version"]
