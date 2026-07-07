import io
from pathlib import Path

#import google.auth
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from birddog.log import get_logger

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def get_drive_service():
    creds = None
    token_path = Path("token.json")
    creds_path = Path("credentials.json")

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)

        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("drive", "v3", credentials=creds)


def list_subdirectories(service, folder_id):
    query = (
        f"'{folder_id}' in parents and trashed = false "
        "and mimeType = 'application/vnd.google-apps.folder'"
    )

    results = []
    page_token = None

    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

        results.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return results

def list_files(service, folder_id):
    query = (
        f"'{folder_id}' in parents and trashed = false "
        "and mimeType != 'application/vnd.google-apps.folder'"
    )
    results = []
    page_token = None

    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, size)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )

        results.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return results


def download_xlsx_file(service, file_id, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)

    with io.FileIO(output_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return str(output_path)


def export_file(service, file_id, output_path, export_mime_type="application/pdf"):
    request = service.files().export_media(fileId=file_id, mimeType=export_mime_type)
    fh = io.FileIO(output_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        status, done = downloader.next_chunk()
        print(f"Export progress: {int(status.progress() * 100)}%")

    fh.close()
    return output_path

DIR_IDS_2_IMPORT = [
    "1Neq2C_jO9_QXnbK1rFHzvsLKRkJAtyXc",
    "1q0_wE1x9dfOCwwULqYnqZQBPu-BlBgGo",
    "1KNa4znKERb2HCqZ-Imq_ZDLTvZt6rNRj",
    "1aYA55DQ8K5_bz7S6fVQ4yGhCI5Fxx0JS",
    "1jvMUXVrenXvw8pmQF6x2sDdMqzkMAiK7",
    "1E8dJJ2dSCaCHdSYBb4EbX9x-Yw-uLfTL",
    "1ND8CzlF5Rx0ZIgKNv4L_65i_ZfXu9n1d",
    "1bDQty5_b9NJCNbtutMFyBkEkbRa_gHmy",
    "1Rk0DWy_PAa5Hl42guQrfm-BKdCda8_LD",
    "1vdVaFMeefwYuz8z4crwXgdC2WDxqo8-S",
    "1e4SsBvqQOLWOdlHWdVZRJuHLWYgrVUvj",
    "1ko1pUQNY9_ZyhD64HfClrgvRsetTlM1s",
    "1rNITVXVcqi5QV0kGEsBFEPT552qZTU9w",
    "1sptrr-x41OsVuVF-K-TDBZACcigBvrqp",
    "1U8ZXAY6BLzQ7Hp7LUFKpYx-V2hJLWh9A",
    "1hJETIJN964b_-x0PzXqJBN50ORfKOxSz",
    "18LLp7GxS43Ex_ChMEhp4f1q4Lgfa363T",
    "1ov1itVBT-LoI6iy15iNuDRig31Qp9Rey",
    "1ci3uoHgh19KFkfwh9bian0h2tXetNd8H",
    "1jMGlaSh-FCwJAbiX3fIO3mX7ly4v5qao",
    "1uFZVY8-WUDBDn4W-NxFArllw_ZUjApt1",
    "162xSBARsnOOUx1gDCV5UX60Up68NGZqE",
    "1VxgETiG7w6fVdUUkaFGg0QYDRz3916yB",
    "1oe9IPLtClGGPrva2nITQvA72u3VdYyHq",
    "1pe9XgmyCCVmuzKDza0nkHQoG-6PmxmJ9",
    "1jYhnmOosM306mVUg4oc_CqPpQlkITmZs",
    "1j2aULe_FyGOpyDdvr9EDoDtwIMswXWT9",
    "1At3sBXgiJ7lqzA-iZZn6AV8xNNBosmFg",
    "1s2SAAJxTx829WlIQVIIuhVYwuw3cU2GQ",
    "1WJ5sNx-9hxa9we3tl2r0Qz1jyjxlEg7p",
    "1YEy1Nud8xdHiETrRPLVpWjoLBUqh05gb",
    "1siTuqvmaszu8LNFneoFsMswKr2I9MPic",
    "1Jt3g7Af8Zwq_Aon_WI66rQjfk2Fs3UJp",
    "1IS5kaao1LXqhfULujo8KQ96ILQ0leVz-",
    "1Qlw33nrlH1dSnuxi4qqItf2Hj68cGXXC",
    "1IZmDF1ow03DBeKBjoNU3NkxK6MApNRmg",
    "1AuCpKPtXOIQWGCv8V_NnZ-46mCoRvWNg",
    "1aCN9ptGNJWJdBF_L3UzcZ_NR4CuCzOzk",
    "1Olm3VtPtqpqLzRnb2T9C3MjJ-eLLOZmW",
    "14GqeQXoviowXfFLCGd-i_5_mu8YPDgCt",
    "1UUWYzglX8ZVghxqgXM-1nCZZUWdpcfeH",
    "1YWGp_V0b2F2aQJso-tgAXspw_PsKmoR_",
    "10KZPliXt3zLR7hg7NntHCIu7cXSFtCwF",
    "17fJf5ZmX7xhDg6mJekep2P4fFzUqHANg",
    "1-y4urktd_CBUjr0yWNfVgz7lOG-PWMen",
    "1GT0O7DHRk5Xf3vxR-Oa3jo9GrVyWWwOa",
    "1MrHFFAbkXIwcvVlvJ77runlzO5R-BiZw"
]

def download_files_from_dir(service, dir_id, output_dir):
    print(f"\nDownloading files from folder with ID={dir_id}")
    for item in list_files(service, dir_id):
        name = item["name"]
        if name.endswith(".xlsx"):
            print(item["name"], item["id"], item.get("mimeType"))
            download_xlsx_file(service, item["id"], f"{output_dir}/{item['name']}")

def download_all_files(service, output_dir):
    for dir_id in DIR_IDS_2_IMPORT:
        download_files_from_dir(service, dir_id, output_dir)


def download_missing_files(service, folder_id, output_path, logger):
    output_dir = Path(output_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    response = (
        service.files()
        .list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive",
            fields="files(id, name, mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )

    files = response.get("files", [])

    downloaded_files = []
    for item in files:
        name = item["name"]
        mime_type = item["mimeType"]
        file_id = item["id"]
        local_file = output_dir / name

        if local_file.exists():
            # the file is already there - skip it
            continue

        if mime_type == "application/vnd.google-apps.folder":
            continue

        if mime_type.startswith("application/vnd.google-apps."):
            continue

        downloaded_files.append(local_file)
        year_posit = name.find('20')
        if year_posit == -1:
            logger.error(f"File name {name} does not contain a year")
        else:
            # delete the previous versions of this file
            name_beginning = name[0: year_posit]
            for p in output_dir.iterdir():
                if p.is_file() and p.name.startswith(name_beginning):
                    logger.info(f"Deleting the file {p.name}")
                    p.unlink()

        logger.info(f"Downloading the file {name}")
        download_xlsx_file(service, file_id, local_file)

    return downloaded_files
                
def download_all_missing_files(service, output_dir, logger):
    downloaded_files = []
    for dir_id in DIR_IDS_2_IMPORT:
        current_downloaded = download_missing_files(service, dir_id, output_dir, logger)
        downloaded_files.append(current_downloaded)

    return downloaded_files


if __name__ == "__main__":
    service_gl = get_drive_service()
    output_path = r"C:\jewishGen\Import2DB\SourceSpreadsheets\All"
    _logger = get_logger()
    download_all_missing_files(service_gl, output_path, _logger)
    
#    _logger = get_logger()
#    folder_id_gl = "1MrHFFAbkXIwcvVlvJ77runlzO5R-BiZw"
#    output_path = r"C:\jewishGen\Import2DB\SourceSpreadsheets\All"
#    download_missing_files(service_gl, folder_id_gl, output_path, _logger)

#    folder_id_gl = "16yGo669zLWYmZlUqoWGe8ZM7wywsOpDQ"

#    print("Subdirectories:")
#    for item_gl in list_subdirectories(service_gl, folder_id_gl):
#        print(item_gl["name"], item_gl["id"])

#    print("\nFiles:")
#    for item_gl in list_files(service_gl, folder_id_gl):
#        print(item_gl["name"], item_gl["id"], item_gl.get("mimeType"))

    # Example download by file ID
    # download_file(service, "YOUR_FILE_ID", "downloaded_file.ext")

    # Example export of a Google Doc/Sheet/Slide
    # export_file(service, "YOUR_GOOGLE_DOC_FILE_ID", "exported.pdf")