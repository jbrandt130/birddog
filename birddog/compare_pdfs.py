"""
Compare two PDF files from URLs to determine if they are almost identical.

Steps:
1. Download the files to a temporary directory.
2. Compare the page count.
3. If the page count is the same, save the first pages again in the temporary directory and compare them using dhash.
4. If these match and there are more than 1 page, do the same with a random page.
"""

import os
import random
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import requests
from comparing_images import (
    compute_dhash,
    convert_pdf_page_to_jpg,
    get_pdf_page_count,
    hamming_distance,
)


def download_wikimedia_file(url: str, filepath: str) -> bool:
    """Downloads a file or page image from Wikimedia Commons URLs.
    Args:
        url (str): The direct URL of the file or image to download.
        filepath (str): Path where the downloaded file will be saved.
    Returns:
        str: Absolute path to the saved file.
    """
    # Decode and safely re-encode the path preserving URL structures
    parts = urlsplit(url)
    decoded_path = unquote(parts.path)
    encoded_path = quote(decoded_path, safe="/,()")
    clean_url = (
            f"{parts.scheme}://{parts.netloc}{encoded_path}"
            + (f"?{parts.query}" if parts.query else "")
    )

    # Wikimedia requires a distinct User-Agent with contact details
    headers = {
        "User-Agent": "ArchiveDownloaderScript/1.0 (https://github.com/ztatyan; azaslavsky@jewishgen.org)"
    }

    try:
        # 3. Request file with stream=True for large files
        response = requests.get(clean_url, headers=headers, stream=True)
        response.raise_for_status()

        # Stream chunks to local file
        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):  # 1 MB chunks
                if chunk:
                    f.write(chunk)
        print(f"Successfully downloaded: {filepath}")
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, requests.RequestException) as e:
        print(f"Error downloading {url}: {e}")
        return False

def compare_local_pdfs(results:dict, pdf1_path:str, pdf2_path:str, temp_dir:str, threshold: int = 5) -> dict:
    # Step 2: Compare page count
    print("Step 2: Comparing page counts...")
    page_count1 = get_pdf_page_count(pdf1_path)
    page_count2 = get_pdf_page_count(pdf2_path)

    print(f"  PDF 1: {page_count1} pages")
    print(f"  PDF 2: {page_count2} pages")

    if page_count1 != page_count2:
        results["details"]["page_count1"] = page_count1
        results["details"]["page_count2"] = page_count2
        results["details"]["page_count_diff"] = abs(page_count1 - page_count2)
        results["comparison_result"] = "Different pdf files"
        print("  ✗ Page counts do not match")
        return results

    results["page_counts_match"] = True
    results["details"]["page_count"] = page_count1
    print(f"  ✓ Page counts match: {page_count1} pages")

    # Step 3: Compare first pages using dhash
    print("Step 3: Comparing first pages using dhash...")
    page1_path = os.path.join(temp_dir, "page1_pdf1.jpg")
    page2_path = os.path.join(temp_dir, "page1_pdf2.jpg")

    convert_pdf_page_to_jpg(pdf1_path, 1, page1_path)
    convert_pdf_page_to_jpg(pdf2_path, 1, page2_path)

    hash1 = compute_dhash(page1_path)
    hash2 = compute_dhash(page2_path)
    distance = hamming_distance(hash1, hash2)

    print(f"  PDF 1 page 1 hash: {hash1}")
    print(f"  PDF 2 page 1 hash: {hash2}")
    print(f"  Hamming distance: {distance}")

    if distance <= threshold:
        results["first_pages_match"] = True
        results["details"]["first_page_hash1"] = hash1
        results["details"]["first_page_hash2"] = hash2
        results["details"]["first_page_distance"] = distance
        print(f"  ✓ First pages match (distance <= {threshold})")
    else:
        results["details"]["first_page_distance"] = distance
        results["comparison_result"] = "Different pdf files"
        print(f"  ✗ First pages differ (distance > {threshold})")
        return results

    # Step 4: If more than 1 page, compare a random page
    if page_count1 > 1:
        print("Step 4: Comparing a random page...")
        random_page = random.randint(2, page_count1)  # Avoid page 1 (already compared)
        print(f"  Comparing page {random_page}...")

        pageN1_path = os.path.join(temp_dir, f"page{random_page}_pdf1.jpg")
        pageN2_path = os.path.join(temp_dir, f"page{random_page}_pdf2.jpg")

        convert_pdf_page_to_jpg(pdf1_path, random_page, pageN1_path)
        convert_pdf_page_to_jpg(pdf2_path, random_page, pageN2_path)

        hashN1 = compute_dhash(pageN1_path)
        hashN2 = compute_dhash(pageN2_path)
        distanceN = hamming_distance(hashN1, hashN2)

        print(f"  PDF 1 page {random_page} hash: {hashN1}")
        print(f"  PDF 2 page {random_page} hash: {hashN2}")
        print(f"  Hamming distance: {distanceN}")

        if distanceN <= threshold:
            results["random_page_match"] = True
            results["details"]["random_page_number"] = random_page
            results["details"]["random_page_hash1"] = hashN1
            results["details"]["random_page_hash2"] = hashN2
            results["details"]["random_page_distance"] = distanceN
            print(f"  ✓ Random page matches (distance <= {threshold})")
        else:
            results["details"]["random_page_distance"] = distanceN
            results["comparison_result"] = "Different pdf files"
            print(f"  ✗ Random page differs (distance > {threshold})")
            return results
    else:
        results["details"]["random_page_skipped"] = "Only 1 page in document"
        print("  ⊘ Only 1 page - skipping random page comparison")

    # All checks passed
    results["comparison_result"] = "PDF files are almost identical"
    print("\n✓ PDF files are ALMOST IDENTICAL")
    return results


def compare_online_pdfs(url1: str, url2: str, threshold: int = 5) -> dict:
    """
    Compare two PDF files from URLs.

    Args:
        url1: URL of the first PDF file.
        url2: URL of the second PDF file.
        threshold: Hamming distance threshold for considering images similar (default: 5).

    Returns:
        Dictionary with comparison results.
    """
    results = {
        "downloaded": False,
        "page_counts_match": False,
        "first_pages_match": False,
        "random_page_match": False,
        "comparison_result": 'Error',
        "details": {"page_count1":0, "error":''},
    }

    with tempfile.TemporaryDirectory() as temp_dir:
        # Step 1: Download files to temporary directory
        print("Step 1: Downloading PDF files...")
        pdf1_path = os.path.join(temp_dir, "pdf1.pdf")
        pdf2_path = os.path.join(temp_dir, "pdf2.pdf")

        if not download_wikimedia_file(url1, pdf1_path):
            results["details"]["error"] = "Failed to download first PDF"
            return results

        if not download_wikimedia_file(url2, pdf2_path):
            results["details"]["error"] = "Failed to download second PDF"
            return results

        results["downloaded"] = True
        print("  ✓ Both files downloaded successfully")

        results = compare_local_pdfs(results, pdf1_path, pdf2_path, temp_dir, threshold)

        #delete temporary files
        Path(pdf1_path).unlink(missing_ok=True)
        Path(pdf2_path).unlink(missing_ok=True)

    return results


def test_comparing(url1:str, url2:str):
    print("Comparing PDFs:")
    print(f"  URL 1: {url1}")
    print(f"  URL 2: {url2}")
    print()

    results = compare_online_pdfs(url1, url2)

    print("\n" + "=" * 50)
    print(f"FINAL RESULT: {results["comparison_result"]}")


if __name__ == "__main__":
#    url1_ = "https://upload.wikimedia.org/wikipedia/commons/1/1f/ДАОО_1-2-10_Про_євреїв,_які_висловили_бажання_перейти_з_білоруських_губерній_в_єврейські_колонії_Херсонської_губернії_(1838).pdf"#    url1_ = "https://upload.wikimedia.org/wikipedia/commons/1/1f/%D0%94%D0%90%D0%9E%D0%9E_1-2-10_%D0%9F%D1%80%D0%BE_%D1%94%D0%B2%D1%80%D0%B5%D1%97%D0%B2%2C_%D1%8F%D0%BA%D1%96_%D0%B2%D0%B8%D1%81%D0%BB%D0%BE%D0%B2%D0%B8%D0%BB%D0%B8_%D0%B1%D0%B0%D0%B6%D0%B0%D0%BD%D0%BD%D1%8F_%D0%BF%D0%B5%D1%80%D0%B5%D0%B9%D1%82%D0%B8_%D0%B7_%D0%B1%D1%96%D0%BB%D0%BE%D1%80%D1%83%D1%81%D1%8C%D0%BA%D0%B8%D1%85_%D0%B3%D1%83%D0%B1%D0%B5%D1%80%D0%BD%D1%96%D0%B9_%D0%B2_%D1%94%D0%B2%D1%80%D0%B5%D0%B9%D1%81%D1%8C%D0%BA%D1%96_%D0%BA%D0%BE%D0%BB%D0%BE%D0%BD%D1%96%D1%97_%D0%A5%D0%B5%D1%80%D1%81%D0%BE%D0%BD%D1%81%D1%8C%D0%BA%D0%BE%D1%97_%D0%B3%D1%83%D0%B1%D0%B5%D1%80%D0%BD%D1%96%D1%97_%281838%29.pdf?utm_source=commons.wikimedia.org&utm_campaign=index&utm_content=original"
#    url2_ = "https://upload.wikimedia.org/wikipedia/commons/0/07/%D0%94%D0%90%D0%9E%D0%9E_16-124-15899_%D0%9F%D1%80%D0%BE_%D0%B2%D0%BD%D0%B5%D1%81%D0%B5%D0%BD%D0%BD%D1%8F_%D0%B2_%D0%9E%D0%B4%D0%B5%D1%81%D1%96_%D0%B4%D0%BE_%D0%BC%D0%B5%D1%82%D1%80%D0%B8%D1%87%D0%BD%D0%B8%D1%85_%D0%BA%D0%BD%D0%B8%D0%B3_%D0%B7%D0%BC%D1%96%D0%BD%2C_%D1%89%D0%BE_%D1%81%D1%82%D0%BE%D1%81%D1%83%D1%8E%D1%82%D1%8C%D1%81%D1%8F_%D1%82%D0%B8%D1%85%2C_%D1%85%D1%82%D0%BE_%D0%BF%D1%80%D0%B8%D0%B9%D0%BD%D1%8F%D0%B2_%D0%BF%D1%80%D0%B0%D0%B2%D0%BE%D1%81%D0%BB%D0%B0%D0%B2%27%D1%8F_%281916%29.pdf?utm_source=uk.wikisource.org&utm_campaign=index&utm_content=original"
#    test_comparing(url1_, url2_)

    results_ = {
        "page_counts_match": False,
        "first_pages_match": False,
        "random_page_match": False,
        "comparison_result": 'Error',
        "details": {"page_count1":0, "error":''},
    }
    pdf1_path_ = r"C:\Users\user\Downloads\kenguru_resized.pdf"
    pdf2_path_ = r"C:\Users\user\Downloads\kenguru_orig.pdf"
    dir_ = r"C:\Users\user\Downloads"
    results_ = compare_local_pdfs(results_, pdf1_path_, pdf2_path_, dir_, 5)
    print(results_)