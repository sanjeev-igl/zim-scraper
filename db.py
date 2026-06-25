import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.errors import ConnectionFailure

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "spot_pricing")
MONGO_COLLECTION = os.getenv("MONGO_COLLECTION", "sailings")


def push_to_mongo(records: list[dict], pol: str, pod: str) -> int:
    """
    Upsert scraped sailing records into MongoDB.
    Uses (vessel, etd, pol, pod) as the unique key to avoid duplicates.
    Returns the number of records upserted or modified.
    """
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
    except ConnectionFailure as exc:
        raise RuntimeError(
            f"Cannot reach MongoDB at {MONGO_URI}. "
            "Make sure mongod is running."
        ) from exc

    col = client[MONGO_DB][MONGO_COLLECTION]
    scraped_at = datetime.now(timezone.utc)

    ops = []
    for record in records:
        filter_key = {
            "vessel": record.get("vessel", ""),
            "etd": record.get("etd", ""),
            "pol": record.get("pol", pol),
            "pod": record.get("pod", pod),
        }
        update = {"$set": {**record, "scraped_at": scraped_at}}
        ops.append(UpdateOne(filter_key, update, upsert=True))

    if not ops:
        return 0

    result = col.bulk_write(ops, ordered=False)
    client.close()
    return result.upserted_count + result.modified_count
