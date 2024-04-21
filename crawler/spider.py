"""
spider.py — Selenium-based crawl worker that runs on a QThread.

Signals
-------
log(str)              — human-readable status message
progress(int)         — estimated completion 0-100
stats(int, int, int)  — (pages_crawled, errors, images_saved)
done()                — emitted once after the crawl loop exits
"""

import json
import logging
import os
import queue
import time
from urllib.parse import urljoin, urlparse

import requests
from PyQt6.QtCore import QThread, pyqtSignal
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager

from . import db
from .utils import can_fetch, normalize, safe_filename, url_folder

logger = logging.getLogger(__name__)


class Spider(QThread):
    log      = pyqtSignal(str)
    progress = pyqtSignal(int)
    stats    = pyqtSignal(int, int, int)
    done     = pyqtSignal()

    def __init__(
        self,
        start_url: str,
        *,
        max_depth: int = 3,
        rate_delay: float = 1.5,
        stay_on_domain: bool = True,
        respect_robots: bool = True,
        extract_links: bool = True,
        extract_images: bool = True,
        extract_text: bool = True,
        extract_metadata: bool = True,
        save_html: bool = False,
        output_folder: str = "output",
    ) -> None:
        super().__init__()
        self.start_url       = normalize(start_url)
        self.max_depth       = max_depth
        self.rate_delay      = rate_delay
        self.stay_on_domain  = stay_on_domain
        self.respect_robots  = respect_robots
        self.extract_links   = extract_links
        self.extract_images  = extract_images
        self.extract_text    = extract_text
        self.extract_metadata = extract_metadata
        self.save_html       = save_html
        self.output_folder   = output_folder

        self._start_domain = urlparse(self.start_url).netloc
        self._visited: set[str] = set()
        self._queue: queue.Queue[tuple[str, int]] = queue.Queue()
        self._queue.put((self.start_url, 0))
        self._stop = False

        self._crawled = 0
        self._errors  = 0
        self._images  = 0

    # ------------------------------------------------------------------
    # Thread control
    # ------------------------------------------------------------------

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        os.makedirs(self.output_folder, exist_ok=True)

        options = Options()
        for arg in ("--headless", "--disable-gpu", "--no-sandbox",
                    "--disable-dev-shm-usage"):
            options.add_argument(arg)

        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()),
            options=options,
        )
        try:
            self._loop(driver)
        finally:
            driver.quit()
            self.done.emit()

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def _loop(self, driver) -> None:
        while not self._queue.empty() and not self._stop:
            url, depth = self._queue.get()
            url = normalize(url)

            if depth > self.max_depth or url in self._visited:
                continue
            if self.stay_on_domain and urlparse(url).netloc != self._start_domain:
                continue
            if not can_fetch(url, respect=self.respect_robots):
                self.log.emit(f"Blocked by robots.txt: {url}")
                continue

            self._visited.add(url)

            try:
                driver.get(url)
                time.sleep(self.rate_delay)

                title = (driver.title or "Untitled").strip()
                folder = url_folder(self.output_folder, url)
                os.makedirs(folder, exist_ok=True)

                self._save_text(folder, "url.txt", url)
                self._save_text(folder, "title.txt", title)

                links: list[str] = []
                if self.extract_links:
                    links = self._get_links(driver, url)
                    self._save_text(folder, "links.txt", "\n".join(links))

                if self.extract_images:
                    img_urls = self._get_images(driver)
                    downloaded = self._download_images(img_urls, folder)
                    self._images += len(downloaded)
                    self._save_text(folder, "images.txt", "\n".join(downloaded))

                if self.extract_text:
                    body = driver.find_element(By.TAG_NAME, "body").text.strip()
                    self._save_text(folder, "text.txt", body)

                if self.extract_metadata:
                    meta = self._get_metadata(driver)
                    self._save_json(folder, "metadata.json", meta)

                if self.save_html:
                    self._save_text(folder, "source.html", driver.page_source)

                db.upsert(url, title, depth)

                self._crawled += 1
                self.log.emit(f"✅ [{depth}/{self.max_depth}] {url}  →  {title}")
                self.stats.emit(self._crawled, self._errors, self._images)
                self.progress.emit(self._estimate_progress())

                for link in links:
                    if link not in self._visited:
                        self._queue.put((link, depth + 1))

            except Exception as exc:
                self._errors += 1
                logger.exception("Error crawling %s", url)
                self.log.emit(f"❌ Error ({url}): {exc}")
                self.stats.emit(self._crawled, self._errors, self._images)

        self.progress.emit(100)

    # ------------------------------------------------------------------
    # Progress estimation
    # ------------------------------------------------------------------

    def _estimate_progress(self) -> int:
        total = self._crawled + self._queue.qsize()
        return min(99, int(self._crawled / max(total, 1) * 100))

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    def _get_links(self, driver, base_url: str) -> list[str]:
        raw = [
            a.get_attribute("href")
            for a in driver.find_elements(By.TAG_NAME, "a")
            if a.get_attribute("href")
        ]
        return sorted({normalize(urljoin(base_url, h)) for h in raw})

    def _get_images(self, driver) -> list[str]:
        return list({
            img.get_attribute("src")
            for img in driver.find_elements(By.TAG_NAME, "img")
            if img.get_attribute("src")
        })

    def _get_metadata(self, driver) -> dict:
        return {
            m.get_attribute("name"): m.get_attribute("content")
            for m in driver.find_elements(By.TAG_NAME, "meta")
            if m.get_attribute("name")
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _save_text(folder: str, name: str, data: str) -> None:
        if data:
            with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                f.write(data)

    @staticmethod
    def _save_json(folder: str, name: str, data: dict) -> None:
        if data:
            with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

    def _download_images(self, urls: list[str], folder: str) -> list[str]:
        img_dir = os.path.join(folder, "images")
        os.makedirs(img_dir, exist_ok=True)
        saved: list[str] = []
        for url in urls:
            try:
                resp = requests.get(url, timeout=8)
                resp.raise_for_status()
                name = safe_filename(url)
                with open(os.path.join(img_dir, name), "wb") as f:
                    f.write(resp.content)
                saved.append(name)
            except Exception as exc:
                logger.warning("Image download failed (%s): %s", url, exc)
                self._errors += 1
        return saved
