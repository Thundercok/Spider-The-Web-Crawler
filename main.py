"""
main.py — Entry point. Run with: python -m crawler
"""

import logging
import sys

from PyQt6.QtWidgets import QApplication

from crawler.app import CrawlerApp
from crawler.style import QSS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(QSS)
    window = CrawlerApp()
    window.show()
    sys.exit(app.exec())
