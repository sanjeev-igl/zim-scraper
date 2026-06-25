import json
from pathlib import Path

from db import push_to_mongo

OUTPUT_DIR = Path("output")

json_files = sorted(OUTPUT_DIR.glob("*.json"))

if not json_files:
    print("No JSON files found in output/")
else:
    total = 0
    for f in json_files:
        try:
            records = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(records, list) or not records:
                print(f"  SKIP {f.name} (empty or not a list)")
                continue
            pol = records[0].get("pol", "")
            pod = records[0].get("pod", "")
            changed = push_to_mongo(records, pol, pod)
            print(f"  OK   {f.name}  →  {changed} record(s) upserted/updated")
            total += changed
        except Exception as e:
            print(f"  ERR  {f.name}: {e}")
    print(f"\nDone. {total} total record(s) pushed to MongoDB.")
