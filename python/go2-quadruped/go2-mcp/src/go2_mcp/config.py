import os

MOTION_BASE_URL = os.environ.get("GO2_MOTION_URL", "http://192.168.123.18:3201")
BRAIN_BASE_URL = os.environ.get("GO2_BRAIN_URL", "http://192.168.123.18:3300")

HTTP_TIMEOUT_S = float(os.environ.get("GO2_MCP_HTTP_TIMEOUT", "2.0"))
