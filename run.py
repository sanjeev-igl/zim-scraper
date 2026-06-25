import asyncio
import json
from datetime import datetime
from pathlib import Path

from scraper import ZimScraper
from db import push_to_mongo

CONFIG_FILE = Path("config.json")
OUTPUT_DIR = Path("output")

DEFAULT_CONFIG = {
    "search": {
        "origin": "INNSA",
        "destination": "CNSHA",
        "container_type": "DV40",
        "quantity": 1,
    },
    "start_url": "",
    "headless": False,
    "slow_mo_ms": 500,
    "debug": False,
    "max_pages": 1,
    "timeout_seconds": 600,
}


def main() -> None:
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        print(
            "config.json created with defaults.\n"
            "Edit 'origin', 'destination', and optionally 'start_url', then run again."
        )
        return

    config = json.loads(CONFIG_FILE.read_text())
    OUTPUT_DIR.mkdir(exist_ok=True)

    scraper = ZimScraper(
        headless=config.get("headless", False),
        slow_mo=config.get("slow_mo_ms", 300),
        debug=config.get("debug", False),
        manual_mode=config.get("manual_mode", True),
        max_pages=config.get("max_pages", 1),
        timeout_seconds=config.get("timeout_seconds", 300),
    )

    print("Starting ZIM spot pricing scraper…\n")
    results = asyncio.run(scraper.scrape(config))

    # Prefer full port names from results (e.g. "NHAVA SHEVA, INDIA");
    # fall back to the short codes from config if results are empty.
    if results:
        first = results[0] if isinstance(results[0], dict) else results[0]
        pol = (first.get("pol") or "").strip() if isinstance(first, dict) else (first.pol or "").strip()
        pod = (first.get("pod") or "").strip() if isinstance(first, dict) else (first.pod or "").strip()
    else:
        pol = config.get("search", {}).get("origin", "unknown").upper()
        pod = config.get("search", {}).get("destination", "unknown").upper()

    def _sanitize(name: str) -> str:
        """Replace characters that are invalid in Windows filenames."""
        return name.replace(",", "").replace(" ", "_").replace("/", "-")

    date_str = datetime.now().strftime("%d-%b-%Y")        # e.g. 22-Jun-2026
    base_name = f"{_sanitize(pol)}_to_{_sanitize(pod)}_{date_str}"

    # If the same route was already scraped today, add a counter rather than overwrite.
    out_path = OUTPUT_DIR / f"{base_name}.json"
    counter = 2
    while out_path.exists():
        out_path = OUTPUT_DIR / f"{base_name}_{counter}.json"
        counter += 1

    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nDone. {len(results)} sailings saved → {out_path}")

    try:
        changed = push_to_mongo(results, pol, pod)
        print(f"MongoDB: {changed} record(s) upserted/updated in '{out_path.stem}'")
    except RuntimeError as exc:
        print(f"[MongoDB skipped] {exc}")


if __name__ == "__main__":
    main()
