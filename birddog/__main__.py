import sys
from birddog.wiki import update_master_archive_list

def main():
    if len(sys.argv) < 2:
        print("Usage: python -m birddog <command>")
        sys.exit(1)

    command = sys.argv[1]

    if command == "update_master_archive_list":
        update_master_archive_list()
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

if __name__ == "__main__":
    main()
