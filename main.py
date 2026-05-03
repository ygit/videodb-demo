"""Smoke test: connect to VideoDB and list videos in the default collection."""

from dotenv import load_dotenv

load_dotenv(".env")

import videodb


def main() -> None:
    conn = videodb.connect()
    coll = conn.get_collection()
    videos = coll.get_videos()

    print(f"Connected to collection: {coll.name} ({coll.id})")
    print(f"Videos: {len(videos)}")
    for v in videos:
        print(f"  - {v.name} [{v.id}]")


if __name__ == "__main__":
    main()
