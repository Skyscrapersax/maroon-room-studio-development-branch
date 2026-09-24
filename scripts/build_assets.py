"""Stage the existing vendored assets for Vercel's static CDN."""
from pathlib import Path
import shutil

root = Path(__file__).resolve().parents[1]
shutil.copytree(root / "static", root / "public" / "static", dirs_exist_ok=True)
print("Static assets staged in public/static")
