"""
Web Crawler — cleaned & upgraded version
"""

import sys
import os
import sqlite3
import logging
import json
import csv
import queue
import time
import threading
import hashlib
import requests
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton, QTextEdit,
    QLabel, QLineEdit, QCheckBox, QSpinBox, QDoubleSpinBox,
    QGroupBox, QHBoxLayout, QProgressBar, QFileDialog, QGridLayout,
    QSplitter, QFrame,
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QFont

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database helper  (thread-safe: one connection per thread via threading.local)
# ---------------------------------------------------------------------------
DB_FILE = "crawler_results.db"
_thread_local = threading.local()
_db_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection, creating it if needed."""
    if not hasattr(_thread_local, "conn"):
        _thread_local.conn = sqlite3.connect(DB_FILE, check_same_thread=True)
        _thread_local.conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                url   TEXT UNIQUE,
                title TEXT,
                depth INTEGER,
                ts    DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        _thread_local.conn.commit()
    return _thread_local.conn


def db_upsert(url: str, title: str, depth: int) -> None:
    """Insert or replace a crawled URL record (thread-safe)."""
    with _db_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO results (url, title, depth) VALUES (?, ?, ?)",
            (url, title, depth),
        )
        conn.commit()


def db_export_csv(path: str) -> int:
    """Export all results to a CSV file. Returns row count."""
    conn = _get_conn()
    rows = conn.execute("SELECT url, title, depth, ts FROM results ORDER BY id").fetchall()
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["url", "title", "depth", "timestamp"])
        writer.writerows(rows)
    return len(rows)


def db_export_json(path: str) -> int:
    """Export all results to a JSON file. Returns row count."""
    conn = _get_conn()
    rows = conn.execute("SELECT url, title, depth, ts FROM results ORDER BY id").fetchall()
    data = [{"url": r[0], "title": r[1], "depth": r[2], "timestamp": r[3]} for r in rows]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return len(data)


# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Strip fragment and trailing slash for consistent deduplication."""
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return parsed._replace(path=path).geturl()


def safe_filename(url: str, ext: str = "") -> str:
    """Create a collision-free filename from a URL using its MD5 digest."""
    digest = hashlib.md5(url.encode()).hexdigest()[:12]
    base = os.path.basename(urlparse(url).path) or "file"
    name, original_ext = os.path.splitext(base)
    return f"{name}_{digest}{ext or original_ext or '.bin'}"


# ---------------------------------------------------------------------------
# Robots.txt cache
# ---------------------------------------------------------------------------
_robots_cache: dict[str, RobotFileParser] = {}
_robots_lock = threading.Lock()


def can_fetch(url: str, respect_robots: bool, user_agent: str = "*") -> bool:
    if not respect_robots:
        return True
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    with _robots_lock:
        if robots_url not in _robots_cache:
            rp = RobotFileParser()
            rp.set_url(robots_url)
            try:
                rp.read()
            except Exception:
                # If robots.txt is unreachable, allow crawling
                rp = None  # type: ignore[assignment]
            _robots_cache[robots_url] = rp
        rp = _robots_cache[robots_url]
    if rp is None:
        return True
    return rp.can_fetch(user_agent, url)


# ---------------------------------------------------------------------------
# Spider (worker thread)
# ---------------------------------------------------------------------------

class Spider(QThread):
    # Signals
    log         = pyqtSignal(str)         # log message
    progress    = pyqtSignal(int)         # 0-100
    stats       = pyqtSignal(int, int, int)  # crawled, errors, images
    done        = pyqtSignal()            # crawling finished

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
        save_html: bool = True,
        output_folder: str = "output",
    ) -> None:
        super().__init__()
        self.start_url      = normalize_url(start_url)
        self.max_depth      = max_depth
        self.rate_delay     = rate_delay
        self.stay_on_domain = stay_on_domain
        self.respect_robots = respect_robots
        self.extract_links  = extract_links
        self.extract_images = extract_images
        self.extract_text   = extract_text
        self.extract_metadata = extract_metadata
        self.save_html      = save_html
        self.output_folder  = output_folder

        self._start_domain  = urlparse(self.start_url).netloc
        self._visited: set[str] = set()
        self._queue: queue.Queue[tuple[str, int]] = queue.Queue()
        self._queue.put((self.start_url, 0))
        self._stop_flag = False

        # Stats counters
        self._crawled = 0
        self._errors  = 0
        self._images  = 0

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop_flag = True

    # ------------------------------------------------------------------
    def run(self) -> None:
        os.makedirs(self.output_folder, exist_ok=True)
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=options
        )
        try:
            self._crawl(driver)
        finally:
            driver.quit()
            self.done.emit()

    # ------------------------------------------------------------------
    def _crawl(self, driver) -> None:
        while not self._queue.empty() and not self._stop_flag:
            url, depth = self._queue.get()
            url = normalize_url(url)

            if depth > self.max_depth or url in self._visited:
                continue

            # Domain guard
            if self.stay_on_domain and urlparse(url).netloc != self._start_domain:
                continue

            # Robots.txt guard
            if not can_fetch(url, self.respect_robots):
                self.log.emit(f"🚫 Blocked by robots.txt: {url}")
                continue

            self._visited.add(url)

            try:
                driver.get(url)
                time.sleep(self.rate_delay)

                title = (driver.title or "Untitled Page").strip()

                # Build output folder: <output>/<domain>/<md5_of_url>/
                domain_slug = urlparse(url).netloc.replace(".", "_")
                url_hash    = hashlib.md5(url.encode()).hexdigest()[:10]
                url_folder  = os.path.join(self.output_folder, domain_slug, url_hash)
                os.makedirs(url_folder, exist_ok=True)

                # Save URL reference
                self._save_text(url_folder, "url.txt", url)
                self._save_text(url_folder, "title.txt", title)

                # Links
                links: list[str] = []
                if self.extract_links:
                    links = self._get_links(driver, url)
                    self._save_text(url_folder, "links.txt", "\n".join(links))

                # Images
                if self.extract_images:
                    image_urls = self._get_images(driver)
                    downloaded = self._download_images(image_urls, url_folder)
                    self._images += len(downloaded)
                    self._save_text(url_folder, "images.txt", "\n".join(downloaded))

                # Body text
                if self.extract_text:
                    text = driver.find_element(By.TAG_NAME, "body").text.strip()
                    self._save_text(url_folder, "text.txt", text)

                # Metadata
                if self.extract_metadata:
                    meta = self._get_metadata(driver)
                    self._save_json(url_folder, "metadata.json", meta)

                # Raw HTML
                if self.save_html:
                    self._save_text(url_folder, "source.html", driver.page_source)

                # Persist to DB
                db_upsert(url, title, depth)

                self._crawled += 1
                self.log.emit(f"✅ [{depth}/{self.max_depth}] {url}  →  {title}")
                self.stats.emit(self._crawled, self._errors, self._images)

                # Estimate progress (rough: queue fill level)
                total_seen = self._crawled + self._queue.qsize()
                pct = min(99, int(self._crawled / max(total_seen, 1) * 100))
                self.progress.emit(pct)

                # Enqueue discovered links
                for link in links:
                    if link not in self._visited:
                        self._queue.put((link, depth + 1))

            except Exception as exc:
                self._errors += 1
                logger.exception("Error processing %s", url)
                self.log.emit(f"❌ Error ({url}): {exc}")
                self.stats.emit(self._crawled, self._errors, self._images)

        self.progress.emit(100)

    # ------------------------------------------------------------------
    # Extraction helpers
    # ------------------------------------------------------------------

    def _get_links(self, driver, base_url: str) -> list[str]:
        raw = [
            a.get_attribute("href")
            for a in driver.find_elements(By.TAG_NAME, "a")
            if a.get_attribute("href")
        ]
        absolute = [normalize_url(urljoin(base_url, href)) for href in raw]
        return sorted(set(absolute))

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
    # Persistence helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _save_text(folder: str, filename: str, data: str) -> None:
        if data:
            with open(os.path.join(folder, filename), "w", encoding="utf-8") as f:
                f.write(data)

    @staticmethod
    def _save_json(folder: str, filename: str, data: dict) -> None:
        if data:
            with open(os.path.join(folder, filename), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

    def _download_images(self, image_urls: list[str], folder: str) -> list[str]:
        img_folder = os.path.join(folder, "images")
        os.makedirs(img_folder, exist_ok=True)
        downloaded: list[str] = []
        for img_url in image_urls:
            try:
                resp = requests.get(img_url, timeout=8)
                resp.raise_for_status()
                # Collision-safe filename
                filename = safe_filename(img_url)
                img_path = os.path.join(img_folder, filename)
                with open(img_path, "wb") as f:
                    f.write(resp.content)
                downloaded.append(filename)
            except Exception as exc:
                logger.warning("Image download failed (%s): %s", img_url, exc)
                self._errors += 1
        return downloaded


# ---------------------------------------------------------------------------
# Main UI
# ---------------------------------------------------------------------------

class CrawlerApp(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._output_folder = "output"
        self._worker: Spider | None = None
        self._init_ui()

    # ------------------------------------------------------------------
    def _init_ui(self) -> None:
        self.setWindowTitle("Web Crawler")
        self.setMinimumSize(860, 680)

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        # --- Title ---
        title = QLabel("🌐 Web Crawler")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Bold))
        root.addWidget(title)

        # --- URL input ---
        url_box = QGroupBox("Target URL")
        url_layout = QHBoxLayout(url_box)
        self.url_input = QLineEdit(placeholderText="https://books.toscrape.com")
        self.url_input.setText("https://books.toscrape.com")
        self.url_input.returnPressed.connect(self.start_crawl)
        url_layout.addWidget(self.url_input)
        root.addWidget(url_box)

        # --- Settings (two columns) ---
        settings_box = QGroupBox("Settings")
        grid = QGridLayout(settings_box)
        grid.setSpacing(8)

        # Extraction checkboxes
        extract_label = QLabel("Extract:")
        extract_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        grid.addWidget(extract_label, 0, 0)

        self.cb_links    = QCheckBox("Links",    checked=True)
        self.cb_images   = QCheckBox("Images",   checked=True)
        self.cb_text     = QCheckBox("Text",     checked=True)
        self.cb_metadata = QCheckBox("Metadata", checked=True)
        self.cb_html     = QCheckBox("Raw HTML", checked=True)
        for i, cb in enumerate([self.cb_links, self.cb_images, self.cb_text,
                                 self.cb_metadata, self.cb_html]):
            grid.addWidget(cb, 0, i + 1)

        # Behaviour checkboxes
        behav_label = QLabel("Behaviour:")
        behav_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        grid.addWidget(behav_label, 1, 0)

        self.cb_same_domain = QCheckBox("Stay on domain", checked=True)
        self.cb_robots      = QCheckBox("Respect robots.txt", checked=True)
        grid.addWidget(self.cb_same_domain, 1, 1, 1, 2)
        grid.addWidget(self.cb_robots,      1, 3, 1, 2)

        # Numerics
        num_label = QLabel("Limits:")
        num_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        grid.addWidget(num_label, 2, 0)

        grid.addWidget(QLabel("Max depth:"), 2, 1)
        self.spin_depth = QSpinBox(minimum=1, maximum=20, value=3)
        grid.addWidget(self.spin_depth, 2, 2)

        grid.addWidget(QLabel("Delay (s):"), 2, 3)
        self.spin_delay = QDoubleSpinBox(minimum=0.5, maximum=30.0,
                                         value=1.5, singleStep=0.5,
                                         decimals=1)
        grid.addWidget(self.spin_delay, 2, 4)

        root.addWidget(settings_box)

        # --- Output folder ---
        folder_layout = QHBoxLayout()
        self.folder_label = QLabel(f"📂 Output: {self._output_folder}")
        folder_btn = QPushButton("Browse…")
        folder_btn.setFixedWidth(80)
        folder_btn.clicked.connect(self._choose_folder)
        folder_layout.addWidget(self.folder_label, 1)
        folder_layout.addWidget(folder_btn)
        root.addLayout(folder_layout)

        # --- Progress + status ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        root.addWidget(self.progress_bar)

        self.status_label = QLabel("Status: Idle")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status_label)

        # --- Stats panel ---
        stats_box = QGroupBox("Statistics")
        stats_layout = QHBoxLayout(stats_box)

        self.lbl_crawled = self._stat_label("Pages crawled", "0")
        self.lbl_errors  = self._stat_label("Errors", "0")
        self.lbl_images  = self._stat_label("Images saved", "0")
        for w in [self.lbl_crawled, self.lbl_errors, self.lbl_images]:
            stats_layout.addWidget(w)
            if w is not self.lbl_images:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.VLine)
                sep.setFrameShadow(QFrame.Shadow.Sunken)
                stats_layout.addWidget(sep)

        root.addWidget(stats_box)

        # --- Action buttons ---
        btn_layout = QHBoxLayout()

        self.btn_start = QPushButton("🚀  Start Crawl")
        self.btn_start.setMinimumHeight(36)
        self.btn_start.clicked.connect(self.start_crawl)

        self.btn_stop = QPushButton("⛔  Stop")
        self.btn_stop.setMinimumHeight(36)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_crawl)

        self.btn_export_csv  = QPushButton("📄  Export CSV")
        self.btn_export_csv.clicked.connect(self._export_csv)

        self.btn_export_json = QPushButton("📋  Export JSON")
        self.btn_export_json.clicked.connect(self._export_json)

        self.btn_clear = QPushButton("🗑  Clear Log")
        self.btn_clear.clicked.connect(self._clear_log)

        for btn in [self.btn_start, self.btn_stop, self.btn_export_csv,
                    self.btn_export_json, self.btn_clear]:
            btn_layout.addWidget(btn)

        root.addLayout(btn_layout)

        # --- Log output ---
        self.log_output = QTextEdit(readOnly=True)
        self.log_output.setFont(QFont("Consolas", 9))
        self.log_output.setMinimumHeight(200)
        root.addWidget(self.log_output, 1)

    # ------------------------------------------------------------------
    # Stat label factory
    # ------------------------------------------------------------------

    @staticmethod
    def _stat_label(heading: str, value: str) -> QLabel:
        lbl = QLabel(f"<b>{value}</b><br/><small>{heading}</small>")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setTextFormat(Qt.TextFormat.RichText)
        return lbl

    def _update_stats(self, crawled: int, errors: int, images: int) -> None:
        self.lbl_crawled.setText(f"<b>{crawled}</b><br/><small>Pages crawled</small>")
        self.lbl_errors.setText(f"<b>{errors}</b><br/><small>Errors</small>")
        self.lbl_images.setText(f"<b>{images}</b><br/><small>Images saved</small>")

    # ------------------------------------------------------------------
    # Folder selection
    # ------------------------------------------------------------------

    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self._output_folder = folder
            self.folder_label.setText(f"📂 Output: {folder}")
            self._log(f"Output folder set to: {folder}")

    # ------------------------------------------------------------------
    # Crawl control
    # ------------------------------------------------------------------

    def start_crawl(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            self._log("⚠️  Please enter a URL.")
            return

        self._worker = Spider(
            url,
            max_depth      = self.spin_depth.value(),
            rate_delay     = self.spin_delay.value(),
            stay_on_domain = self.cb_same_domain.isChecked(),
            respect_robots = self.cb_robots.isChecked(),
            extract_links  = self.cb_links.isChecked(),
            extract_images = self.cb_images.isChecked(),
            extract_text   = self.cb_text.isChecked(),
            extract_metadata = self.cb_metadata.isChecked(),
            save_html      = self.cb_html.isChecked(),
            output_folder  = self._output_folder,
        )
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self.progress_bar.setValue)
        self._worker.stats.connect(self._update_stats)
        self._worker.done.connect(self._on_crawl_done)

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress_bar.setValue(0)
        self.status_label.setText("Status: Crawling…")
        self._log(f"▶ Started crawling: {url}")
        self._worker.start()

    def stop_crawl(self) -> None:
        if self._worker:
            self._worker.stop()
            self.status_label.setText("Status: Stopping…")
            self._log("🛑 Stop requested — finishing current page…")

    def _on_crawl_done(self) -> None:
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.status_label.setText("Status: Idle")
        self._log("✔ Crawl complete.")

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CSV", "results.csv", "CSV Files (*.csv)"
        )
        if path:
            try:
                n = db_export_csv(path)
                self._log(f"📄 Exported {n} rows → {path}")
            except Exception as exc:
                self._log(f"❌ CSV export failed: {exc}")

    def _export_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save JSON", "results.json", "JSON Files (*.json)"
        )
        if path:
            try:
                n = db_export_json(path)
                self._log(f"📋 Exported {n} records → {path}")
            except Exception as exc:
                self._log(f"❌ JSON export failed: {exc}")

    # ------------------------------------------------------------------
    # Log helpers
    # ------------------------------------------------------------------

    def _log(self, text: str) -> None:
        self.log_output.append(text)

    def _clear_log(self) -> None:
        self.log_output.clear()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = CrawlerApp()
    window.show()
    sys.exit(app.exec())
