import numpy as np
import pypdfium2 as pdfium
from PIL import Image
from pypdf import PdfReader


def convert_pdf_page_to_jpg(pdf_path: str, page_number: int, output_jpg_path: str, scale: float = 2.0):
    """
    Converts a specific PDF page to a JPG image using pypdfium2.
    
    :param pdf_path: Path to the input PDF file.
    :param page_number: 1-based index of the page to extract (e.g., 1 for the first page).
    :param output_jpg_path: File path where the output image will be saved.
    :param scale: Render scale factor (2.0 gives ~144 DPI for higher resolution).
    """
    pdf = pdfium.PdfDocument(pdf_path)
    
    # Check if page number is valid
    if page_number < 1 or page_number > len(pdf):
        raise ValueError(f"Page number {page_number} is out of bounds (1-{len(pdf)})")
    
    # pypdfium2 uses 0-based indexing for pages
    page = pdf[page_number - 1]
    
    # Render page to a PIL Image and save as JPG
    image = page.render(scale=scale).to_pil()
    image.save(output_jpg_path, format="JPEG")

# Example usage:
# convert_pdf_page_to_jpg("input.pdf", page_number=1, output_jpg_path="page_1.jpg")

def get_pdf_page_count(pdf_path: str) -> int:
    """Returns the total number of pages in a PDF file using pypdf."""
    reader = PdfReader(pdf_path)
    return len(reader.pages)

# Example usage:
# pages = get_pdf_page_count("example.pdf")
# print(f"Total pages: {pages}")

def compute_dhash(image_input, hash_size: int = 8) -> str:
    """Computes a difference perceptual hash (dHash) for an image or image path.

    Args:
        image_input: Path to image file (str/Path) or a PIL Image object.
        hash_size: Grid width for the hash.

    Returns:
        Hexadecimal string representing the perceptual hash.
    """
    # 1. Load image if a file path is provided
    if not isinstance(image_input, Image.Image):
        image = Image.open(image_input)
    else:
        image = image_input

    # 2. Convert to grayscale and resize to (hash_size + 1, hash_size)
    # Resizing normalizes scale, while grayscale removes color variance.
    # We add 1 pixel to width to calculate hash_size adjacent differences per row.
    resized = image.convert("L").resize(
        (hash_size + 1, hash_size), Image.Resampling.LANCZOS
    )

    # 3. Convert image pixels to a NumPy array
    pixels = np.array(resized, dtype=np.int16)

    # 4. Compute differences: compare adjacent horizontal pixels (left > right)
    # True if left pixel is brighter than right pixel
    difference = pixels[:, :-1] > pixels[:, 1:]

    # 5. Convert boolean matrix to a 64-bit integer bitstring and return as Hex
    flat_diff = difference.flatten()
    decimal_val = 0
    for bit in flat_diff:
        decimal_val = (decimal_val << 1) | int(bit)

    # Format into a padded hex string (16 characters for 64-bit/8x8 grid)
    hex_len = (hash_size * hash_size) // 4
    return f"{decimal_val:0{hex_len}x}"


def hamming_distance(hash1: str, hash2: str) -> int:
    """Calculates the Hamming distance (number of differing bits) between two hex hashes."""
    # Convert hex back to integers and XOR them
    val1 = int(hash1, 16)
    val2 = int(hash2, 16)
    xor_val = val1 ^ val2

    # Count the number of set bits (1s) in the XOR result
    return xor_val.bit_count()
	
if __name__ == "__main__":
    # 1. Compute hashes for two different scans
    hash_scan_a = compute_dhash("doc_scan_low_res.jpg")
    hash_scan_b = compute_dhash("doc_scan_high_res.pdf_page1.png")

    print(f"Hash A: {hash_scan_a}")
    print(f"Hash B: {hash_scan_b}")

    # 2. Calculate the Hamming Distance
    distance = hamming_distance(hash_scan_a, hash_scan_b)
    print(f"Hamming Distance: {distance}")

    # 3. Evaluate Similarity Threshold
    # For a 64-bit hash (8x8 grid):
    # Distance <= 5: Highly likely to be the same document scan.
    # Distance > 10: Significantly different images.
    if distance <= 5:
        print("Match confirmed: Same document scan!")
    else:
        print("Different document scans.")