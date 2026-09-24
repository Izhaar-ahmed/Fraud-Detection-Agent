"""Check the TigerGraph Savanna connection using TG_HOST and TG_SECRET from .env."""
from pathlib import Path
import os
import sys

from dotenv import load_dotenv
import pyTigerGraph as tg

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
host, secret = os.environ.get("TG_HOST", ""), os.environ.get("TG_SECRET", "")
if not host or not secret:
    sys.exit("TG_HOST or TG_SECRET missing from .env")

conn = tg.TigerGraphConnection(host=host, gsqlSecret=secret, tgCloud=True)
try:
    conn.getToken(secret)
    print("token: ok")
except Exception as exc:  # token endpoint shape differs across TG versions
    print(f"token: failed ({type(exc).__name__}: {str(exc)[:200]})")
print(conn.gsql("ls")[:600])
