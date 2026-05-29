"""
Save this file.

If Python is not installed, install Python 3 from https://www.python.org/downloads/.

Open a terminal or command prompt.

Install the needed packages:
pip install pillow requests beautifulsoup4

Import images and convert them to PDF by calling this  from a terminal or command prompt:
python download_convert_to_pdf.py "https://archive.mk.ua/digital-archive/r1002/r1002-001/r1002-001-006/" "downloads" "output.pdf"
Replace here "https://archive.mk.ua/digital-archive/r1002/r1002-001/r1002-001-006/" with your URL,
"downloads" with the path to the download directory, and "output.pdf" with the name of the output PDF file.
"""

from pathlib import Path
from urllib.parse import urljoin, urlparse
import argparse

import requests
from bs4 import BeautifulSoup
from PIL import Image


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff", ".bmp"}


def download_images(page_link, download_dir):
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading images from {page_link}...")
    response = requests.get(page_link, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    links = set()

    for tag in soup.select("img[src], a[href]"):
        url = tag.get("src") or tag.get("href")
        if not url:
            continue

        full_url = urljoin(page_link, url)
        parsed_path = urlparse(full_url).path
        if Path(parsed_path).suffix.lower() in IMAGE_EXTS:
            links.add(full_url)

    if not links:
        print("No images found.")
        return

    for i, img_url in enumerate(sorted(links), 1):
        r = requests.get(img_url, timeout=30)
        r.raise_for_status()

        ext = Path(urlparse(img_url).path).suffix.lower()
        if ext not in IMAGE_EXTS:
            ext = ".jpg"

        path = download_dir / f"{i:03d}{ext}"
        with open(path, "wb") as f:
            f.write(r.content)

    print(f"Downloaded {len(links)} images to {download_dir}")


def images_to_pdf(image_dir, output_pdf_file):
    image_dir = Path(image_dir)
    output_pdf_file = Path(output_pdf_file)

    image_files = sorted(
        f for f in image_dir.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTS
    )

    if not image_files:
        raise ValueError("No image files found")

    images = []
    for f in image_files:
        img = Image.open(f)
        if img.mode != "RGB":
            img = img.convert("RGB")
        images.append(img)

    first, rest = images[0], images[1:]
    first.save(output_pdf_file, save_all=True, append_images=rest)
    print(f"Saved file {output_pdf_file}")


def download_images_convert_to_pdf(page_link, download_dir, output_pdf_file):
    download_images(page_link, download_dir)
    images_to_pdf(download_dir, output_pdf_file)


def main():
    parser = argparse.ArgumentParser(
        description="Download images from a web page and convert them to a PDF."
    )
    parser.add_argument("page_link", help="URL of the web page containing images")
    parser.add_argument("download_dir", help="Folder where images will be saved")
    parser.add_argument("output_pdf_file", help="Output PDF file path")
    args = parser.parse_args()

    download_images_convert_to_pdf(
        args.page_link,
        args.download_dir,
        args.output_pdf_file,
    )


if __name__ == "__main__":
    main()