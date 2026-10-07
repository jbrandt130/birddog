import unittest
from pathlib import Path

from birddog.compare_pdfs import compare_local_pdfs, compare_online_pdfs
from birddog.log import get_logger

UNITTEST_RESOURCE_DIR = 'test/resources'

class TestComparePDFs(unittest.TestCase):

    def test_online_pdf_comparison(self):
        url1_ = "https://upload.wikimedia.org/wikipedia/commons/1/1f/ДАОО_1-2-10_Про_євреїв,_які_висловили_бажання_перейти_з_білоруських_губерній_в_єврейські_колонії_Херсонської_губернії_(1838).pdf"
        url2_ = "https://upload.wikimedia.org/wikipedia/commons/0/07/%D0%94%D0%90%D0%9E%D0%9E_16-124-15899_%D0%9F%D1%80%D0%BE_%D0%B2%D0%BD%D0%B5%D1%81%D0%B5%D0%BD%D0%BD%D1%8F_%D0%B2_%D0%9E%D0%B4%D0%B5%D1%81%D1%96_%D0%B4%D0%BE_%D0%BC%D0%B5%D1%82%D1%80%D0%B8%D1%87%D0%BD%D0%B8%D1%85_%D0%BA%D0%BD%D0%B8%D0%B3_%D0%B7%D0%BC%D1%96%D0%BD%2C_%D1%89%D0%BE_%D1%81%D1%82%D0%BE%D1%81%D1%83%D1%8E%D1%82%D1%8C%D1%81%D1%8F_%D1%82%D0%B8%D1%85%2C_%D1%85%D1%82%D0%BE_%D0%BF%D1%80%D0%B8%D0%B9%D0%BD%D1%8F%D0%B2_%D0%BF%D1%80%D0%B0%D0%B2%D0%BE%D1%81%D0%BB%D0%B0%D0%B2%27%D1%8F_%281916%29.pdf?utm_source=uk.wikisource.org&utm_campaign=index&utm_content=original"

        results = compare_online_pdfs(url1_, url2_)
        self.assertEqual(results["comparison_result"], "Different pdf files")

    def test_local_pdf_comparison(self):
        pdf1_path_ = f'{UNITTEST_RESOURCE_DIR}/unittest_compare_pdf_orig.pdf'
        pdf2_path_ = f'{UNITTEST_RESOURCE_DIR}/unittest_compare_pdf_resized.pdf'

        logger_ = get_logger()
        results = compare_local_pdfs(pdf1_path_, pdf2_path_, UNITTEST_RESOURCE_DIR, logger_, 5)

        # Clean the temporary files
        dir_path = Path(UNITTEST_RESOURCE_DIR)
        for file_path in dir_path.glob("page*_pdf*.*"):
            if file_path.is_file():
                file_path.unlink()

        self.assertEqual(results["comparison_result"], "PDF files are almost identical")


if __name__ == "__main__":
    unittest.main()