"""
Persistent Volume Example

Demonstrates reading and writing to a persistent volume that survives container restarts.
"""

import os
from pathlib import Path

DATA_DIR = Path("/data")


def main():
    print("Persistent Volume Example")
    print("=" * 40)

    # Ensure the data directory exists
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Create and write to foo.md
    foo_path = DATA_DIR / "foo.md"
    content = """# Hello from Persistent Storage

This file was created by the persistent-volume example.

It will survive container restarts because it's stored
in a persistent volume mounted at `/data`.
"""

    print(f"\nWriting to {foo_path}...")
    foo_path.write_text(content)
    print("Done!")

    # Read and display foo.md
    print(f"\nReading from {foo_path}:")
    print("-" * 40)
    print(foo_path.read_text())
    print("-" * 40)

    # List all items in the persistent volume
    print(f"\nContents of {DATA_DIR}:")
    for item in DATA_DIR.iterdir():
        item_type = "DIR " if item.is_dir() else "FILE"
        print(f"  [{item_type}] {item.name}")

    print("\nPersistent volume example complete!")


if __name__ == "__main__":
    main()
