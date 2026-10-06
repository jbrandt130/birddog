"""Compare two PDF files from URLs to determine if they are almost identical.

Steps:
1. Download the files to a temporary directory.
2. Compare the page count.
3. If the page count is the same, save the first pages in the temporary directory
   and compare them using dhash.
4. If these match and there are more than 1 page, compare a random page.
"""

import logging
import random
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import requests
from comparing_images import (
    compute_dhash,
    convert_pdf_page_to_jpg,
    get_pdf_page_count,
    hamming_distance,
)

from birddog.log import get_logger


def download_wikimedia_file(url: str, filepath: str | Path, timeout: int = 30) -> bool:
    """Downloads a file or page image from Wikimedia Commons URLs.

    Args:
        url: The direct URL of the file or image to download.
        filepath: Path where the downloaded file will be saved.
        timeout: Request timeout in seconds.

    Returns:
        bool: True if download succeeded, False otherwise.
    """
    filepath = Path(filepath)
    parts = urlsplit(url)
    decoded_path = unquote(parts.path)
    encoded_path = quote(decoded_path, safe="/,()")
    clean_url = f"{parts.scheme}://{parts.netloc}{encoded_path}"
    if parts.query:
        clean_url += f"?{parts.query}"

    headers = {
        'User-Agent': 'BirddogBot/1.0 (non-commercial research, contact: birddogpound@gmail.com)'
    }

    try:
        response = requests.get(clean_url, headers=headers, stream=True, timeout=timeout)
        response.raise_for_status()

        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        print(f"Successfully downloaded: {filepath}")
        return True
    except requests.RequestException as e:
        print(f"Error downloading {url}: {e}")
        return False


def compare_local_pdfs(
    pdf1_path: str | Path,
    pdf2_path: str | Path,
    temp_dir: str | Path,
    logger: logging.Logger,
    threshold: int = 5,
) -> dict[str, Any]:
    """Compares two local PDF files by page count and visual hash of selected pages."""
    pdf1_path = Path(pdf1_path)
    pdf2_path = Path(pdf2_path)
    temp_dir = Path(temp_dir)

    results: dict[str, Any] = {
        "page_counts_match": False,
        "first_pages_match": False,
        "random_page_match": False,
        "comparison_result": "Error",
        "details": {},
    }

    # 1. Compare page count
    logger.info("Comparing page counts...")
    page_count1 = get_pdf_page_count(str(pdf1_path))
    page_count2 = get_pdf_page_count(str(pdf2_path))

    logger.info("  PDF 1: %d pages", page_count1)
    logger.info("  PDF 2: %d pages", page_count2)

    if page_count1 != page_count2:
        results["details"].update(
            {
                "page_count1": page_count1,
                "page_count2": page_count2,
                "page_count_diff": abs(page_count1 - page_count2),
            }
        )
        results["comparison_result"] = "Different pdf files"
        logger.info("  ✗ Page counts do not match")
        return results

    results["page_counts_match"] = True
    results["details"]["page_count"] = page_count1
    logger.info("  ✓ Page counts match: %d pages", page_count1)

    # 2. Compare first pages using dhash
    logger.info("Comparing first pages using dhash...")
    page1_path = temp_dir / "page1_pdf1.jpg"
    page2_path = temp_dir / "page1_pdf2.jpg"

    convert_pdf_page_to_jpg(str(pdf1_path), 1, str(page1_path))
    convert_pdf_page_to_jpg(str(pdf2_path), 1, str(page2_path))

    hash1 = compute_dhash(str(page1_path))
    hash2 = compute_dhash(str(page2_path))
    distance = hamming_distance(hash1, hash2)

    logger.info("  PDF 1 page 1 hash: %s", hash1)
    logger.info("  PDF 2 page 1 hash: %s", hash2)
    logger.info("  Hamming distance: %d", distance)

    results["details"].update(
        {
            "first_page_hash1": hash1,
            "first_page_hash2": hash2,
            "first_page_distance": distance,
        }
    )

    if distance <= threshold:
        results["first_pages_match"] = True
        logger.info("  ✓ First pages match (distance <= %d)", threshold)
    else:
        results["comparison_result"] = "Different pdf files"
        logger.info("  ✗ First pages differ (distance > %d)", threshold)
        return results

    # 3. If more than 1 page, compare a random page
    if page_count1 > 1:
        logger.info("Comparing a random page...")
        random_page = random.randint(2, page_count1)
        logger.info("  Comparing page %d...", random_page)

        page_n1_path = temp_dir / f"page{random_page}_pdf1.jpg"
        page_n2_path = temp_dir / f"page{random_page}_pdf2.jpg"

        convert_pdf_page_to_jpg(str(pdf1_path), random_page, str(page_n1_path))
        convert_pdf_page_to_jpg(str(pdf2_path), random_page, str(page_n2_path))

        hash_n1 = compute_dhash(str(page_n1_path))
        hash_n2 = compute_dhash(str(page_n2_path))
        distance_n = hamming_distance(hash_n1, hash_n2)

        logger.info("  PDF 1 page %d hash: %s", random_page, hash_n1)
        logger.info("  PDF 2 page %d hash: %s", random_page, hash_n2)
        logger.info("  Hamming distance: %d", distance_n)

        results["details"].update(
            {
                "random_page_number": random_page,
                "random_page_hash1": hash_n1,
                "random_page_hash2": hash_n2,
                "random_page_distance": distance_n,
            }
        )

        if distance_n <= threshold:
            results["random_page_match"] = True
            logger.info("  ✓ Random page matches (distance <= %d)", threshold)
        else:
            results["comparison_result"] = "Different pdf files"
            logger.info("  ✗ Random page differs (distance > %d)", threshold)
            return results
    else:
        results["details"]["random_page_skipped"] = "Only 1 page in document"
        logger.info("  ⊘ Only 1 page - skipping random page comparison")

    # All checks passed
    results["comparison_result"] = "PDF files are almost identical"
    logger.info("\n✓ PDF files are ALMOST IDENTICAL")
    return results


def compare_online_pdfs(url1: str, url2: str, threshold: int = 5) -> dict[str, Any]:
    """Compare two PDF files from URLs."""
    logger = get_logger()

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        pdf1_path = temp_path / "pdf1.pdf"
        pdf2_path = temp_path / "pdf2.pdf"

        logger.info("Downloading PDF files...")
        if not download_wikimedia_file(url1, pdf1_path):
            return {
                "downloaded": False,
                "comparison_result": "Error",
                "details": {"error": "Failed to download first PDF"},
            }

        if not download_wikimedia_file(url2, pdf2_path):
            return {
                "downloaded": False,
                "comparison_result": "Error",
                "details": {"error": "Failed to download second PDF"},
            }

        logger.info("  ✓ Both files downloaded successfully")
        results = compare_local_pdfs(pdf1_path, pdf2_path, temp_path, logger, threshold)
        results["downloaded"] = True
        return results


def test_comparing(url1: str, url2: str) -> None:
    print("Comparing PDFs:")
    print(f"  URL 1: {url1}")
    print(f"  URL 2: {url2}\n")

    results = compare_online_pdfs(url1, url2)

    print("\n" + "=" * 50)
    print(f"FINAL RESULT: {results['comparison_result']}")


if __name__ == "__main__":
    url1_ = "https://upload.wikimedia.org/wikipedia/commons/1/1f/ДАОО_1-2-10_Про_євреїв,_які_висловили_бажання_перейти_з_білоруських_губерній_в_єврейські_колонії_Херсонської_губернії_(1838).pdf"
    url2_ = "https://upload.wikimedia.org/wikipedia/commons/0/07/%D0%94%D0%90%D0%9E%D0%9E_16-124-15899_%D0%9F%D1%80%D0%BE_%D0%B2%D0%BD%D0%B5%D1%81%D0%B5%D0%BD%D0%BD%D1%8F_%D0%B2_%D0%9E%D0%B4%D0%B5%D1%81%D1%96_%D0%B4%D0%BE_%D0%BC%D0%B5%D1%82%D1%80%D0%B8%D1%87%D0%BD%D0%B8%D1%85_%D0%BA%D0%BD%D0%B8%D0%B3_%D0%B7%D0%BC%D1%96%D0%BD%2C_%D1%89%D0%BE_%D1%81%D1%82%D0%BE%D1%81%D1%83%D1%8E%D1%82%D1%8C%D1%81%D1%8F_%D1%82%D0%B8%D1%85%2C_%D1%85%D1%82%D0%BE_%D0%BF%D1%80%D0%B8%D0%B9%D0%BD%D1%8F%D0%B2_%D0%BF%D1%80%D0%B0%D0%B2%D0%BE%D1%81%D0%BB%D0%B0%D0%B2%27%D1%8F_%281916%29.pdf?utm_source=uk.wikisource.org&utm_campaign=index&utm_content=original"
    test_comparing(url1_, url2_)
#    pdf1_path_ = r"C:\Users\user\Downloads\kenguru_resized.pdf"
#    pdf2_path_ = r"C:\Users\user\Downloads\kenguru_orig.pdf"
#    dir_ = r"C:\Users\user\Downloads"
#    logger_ = get_logger()
#    results_ = compare_local_pdfs(pdf1_path_, pdf2_path_, dir_, logger_, 5)
#    print(results_)