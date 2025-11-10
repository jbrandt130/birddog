import sys
from birddog.wiki import update_master_archive_list
from birddog.tracker import process_tracker_unknowns

def main():
    if len(sys.argv) < 2:
        print("Usage: python -m birddog <command>")
        print("       available commands:")
        print("           update_master_archive_list")
        print("           process_tracker_unknowns")
        sys.exit(1)

    command = sys.argv[1]

    if command == "update_master_archive_list":
        update_master_archive_list()
    elif command == "process_tracker_unknowns":
        process_tracker_unknowns()
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)

if __name__ == "__main__":
    main()
