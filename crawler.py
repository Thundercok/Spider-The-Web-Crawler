import sys
import os
import sqlite3
import logging
import json
import queue
import time
import requests
from urllib.parse import urljoin, urlparse
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QPushButton, QTextEdit,
    QLabel, QLineEdit, QCheckBox, QSpinBox, QGroupBox, QHBoxLayout,
    QProgressBar, QFileDialog
)
from PyQt6.QtCore import QThread, pyqtSignal
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Database setup
DB_FILE = "crawler_results.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE,
    title TEXT
)""")
conn.commit()

class Spider(QThread):
    finished = pyqtSignal(str)
    progress = pyqtSignal(int)
    
    def __init__(self, start_url, max_depth=3, extract_links=True, extract_images=True,
                 extract_text=True, extract_metadata=True, save_html=True, output_folder="output"):
        super().__init__()
        self.start_url = start_url
        self.max_depth = max_depth
        self.extract_links = extract_links
        self.extract_images = extract_images
        self.extract_text = extract_text
        self.extract_metadata = extract_metadata
        self.save_html = save_html
        self.output_folder = output_folder
        self.visited_urls = set()
        self.queue = queue.Queue()
        self.queue.put((start_url, 0))
        self.stop_flag = False
        
    def stop(self):
        self.stop_flag = True
        
    def run(self):
        os.makedirs(self.output_folder, exist_ok=True)
        self.crawl()
        
    def crawl(self):
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        
        while not self.queue.empty() and not self.stop_flag:
            url, depth = self.queue.get()
            if depth > self.max_depth or url in self.visited_urls:
                continue
            
            self.visited_urls.add(url)
            try:
                driver.get(url)
                time.sleep(2)
                title = driver.title.strip() or "Untitled Page"
                
                # Create organized folder: output/domain_name/safe_hash/
                domain = urlparse(url).netloc.replace(".", "_")
                path = urlparse(url).path.strip("/").replace("/", "_") or "home"
                safe_hash = str(abs(hash(url)) % (10**8))
                url_folder = os.path.join(self.output_folder, domain, safe_hash)
                os.makedirs(url_folder, exist_ok=True)
                
                # Extract data
                links = sorted(set(self.extract_page_links(driver))) if self.extract_links else []
                images = sorted(set(self.extract_page_images(driver))) if self.extract_images else []
                text = driver.find_element(By.TAG_NAME, "body").text.strip() if self.extract_text else ""
                metadata = self.extract_page_metadata(driver) if self.extract_metadata else {}
                raw_html = driver.page_source if self.save_html else ""
                
                # Save data into clearly named files
                self.save_data(url_folder, "title.txt", title)
                self.save_data(url_folder, "links.txt", "\n".join(links))
                self.save_data(url_folder, "text.txt", text)
                self.save_json(url_folder, "metadata.json", metadata)
                if self.save_html:
                    self.save_data(url_folder, "source.html", raw_html)
                self.save_data(url_folder, "crawl_log.txt", f"URL: {url}\nTitle: {title}\nExtracted {len(links)} links, {len(images)} images.\n")
                
                # Download images as files in an 'images' subfolder
                downloaded_imgs = self.download_images(images, url_folder)
                self.save_data(url_folder, "images.txt", "\n".join(downloaded_imgs))
                
                # Save to database
                cursor.execute("INSERT OR REPLACE INTO results (url, title) VALUES (?, ?)", (url, title))
                conn.commit()
                
                self.finished.emit(f"✅ Crawled: {url} | Saved in {url_folder}")
                
                for link in links:
                    absolute_link = urljoin(url, link)
                    if absolute_link not in self.visited_urls:
                        self.queue.put((absolute_link, depth + 1))
            
            except Exception as e:
                logging.error(f"Error while processing {url}: {e}")
                self.finished.emit(f"❌ Error: {e}")
        
        driver.quit()
        
    def extract_page_links(self, driver):
        return [a.get_attribute("href") for a in driver.find_elements(By.TAG_NAME, "a") if a.get_attribute("href")]
    
    def extract_page_images(self, driver):
        return [img.get_attribute("src") for img in driver.find_elements(By.TAG_NAME, "img") if img.get_attribute("src")]
    
    def extract_page_metadata(self, driver):
        return {meta.get_attribute("name"): meta.get_attribute("content") 
                for meta in driver.find_elements(By.TAG_NAME, "meta") if meta.get_attribute("name")}
    
    def save_data(self, folder, filename, data):
        if data:
            with open(os.path.join(folder, filename), "w", encoding="utf-8") as f:
                f.write(data)
    
    def save_json(self, folder, filename, data):
        if data:
            with open(os.path.join(folder, filename), "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
    
    def download_images(self, image_urls, folder):
        img_folder = os.path.join(folder, "images")
        os.makedirs(img_folder, exist_ok=True)
        downloaded = []
        for img_url in image_urls:
            try:
                response = requests.get(img_url, timeout=5)
                if response.status_code == 200:
                    base_name = os.path.basename(urlparse(img_url).path) or f"image_{len(downloaded)+1}.jpg"
                    img_path = os.path.join(img_folder, base_name)
                    with open(img_path, "wb") as f:
                        f.write(response.content)
                    downloaded.append(base_name)
            except Exception as e:
                logging.error(f"Failed to download image {img_url}: {e}")
                self.finished.emit(f"❌ Image download error: {e}")
        return downloaded

class CrawlerApp(QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()
    
    def initUI(self):
        layout = QVBoxLayout()
        
        self.title_label = QLabel("<h2>🌍 Huynh Nhat Huy's Awesome Web Crawler</h2>")
        layout.addWidget(self.title_label)
        
        self.url_label = QLabel("🔗 Enter URL:")
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("http://books.toscrape.com")
        self.url_input.setText("http://books.toscrape.com")
        self.url_input.returnPressed.connect(self.run_spider)
        layout.addWidget(self.url_label)
        layout.addWidget(self.url_input)
        
        settings_box = QGroupBox("📊 Extraction Settings")
        settings_layout = QHBoxLayout()
        self.extract_links = QCheckBox("Links", checked=True)
        self.extract_images = QCheckBox("Images", checked=True)
        self.extract_text = QCheckBox("Text", checked=True)
        self.extract_metadata = QCheckBox("Metadata", checked=True)
        self.save_html = QCheckBox("HTML", checked=True)
        settings_layout.addWidget(self.extract_links)
        settings_layout.addWidget(self.extract_images)
        settings_layout.addWidget(self.extract_text)
        settings_layout.addWidget(self.extract_metadata)
        settings_layout.addWidget(self.save_html)
        settings_box.setLayout(settings_layout)
        layout.addWidget(settings_box)
        
        self.depth_label = QLabel("📏 Max Depth:")
        self.depth_input = QSpinBox(minimum=1, maximum=10, value=3)
        layout.addWidget(self.depth_label)
        layout.addWidget(self.depth_input)
        
        self.folder_button = QPushButton("📂 Select Output Folder")
        self.folder_button.clicked.connect(self.select_output_folder)
        layout.addWidget(self.folder_button)
        
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)
        
        # Status label to indicate running status
        self.status_label = QLabel("Status: Idle")
        layout.addWidget(self.status_label)
        
        btn_layout = QHBoxLayout()
        self.btn_spider = QPushButton("🚀 Crawl!")
        self.btn_spider.clicked.connect(self.run_spider)
        self.btn_stop = QPushButton("⛔ Stop Crawling")
        self.btn_stop.clicked.connect(self.stop_spider)
        btn_layout.addWidget(self.btn_spider)
        btn_layout.addWidget(self.btn_stop)
        layout.addLayout(btn_layout)
        
        self.output = QTextEdit(readOnly=True)
        layout.addWidget(self.output)
        
        self.setLayout(layout)
        self.setWindowTitle("Huynh Nhat Huy's Awesome Web Crawler")
        self.output_folder = "output"
    
    def select_output_folder(self): 
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.output_folder = folder
            self.output.append(f"Output folder set to: {folder}")
    
    def run_spider(self):
        url = self.url_input.text()
        if url:
            self.status_label.setText("Status: Now Running...")
            self.worker = Spider(
                url,
                max_depth=self.depth_input.value(),
                extract_links=self.extract_links.isChecked(),
                extract_images=self.extract_images.isChecked(),
                extract_text=self.extract_text.isChecked(),
                extract_metadata=self.extract_metadata.isChecked(),
                save_html=self.save_html.isChecked(),
                output_folder=self.output_folder
            )
            self.worker.progress.connect(self.progress_bar.setValue)
            self.worker.finished.connect(self.display_output)
            self.worker.start()
    
    def stop_spider(self):
        if hasattr(self, 'worker'):
            self.worker.stop()
            self.status_label.setText("Status: Stopped")
            self.output.append("🛑 Crawling Stopped.")
    
    def display_output(self, text):
        self.output.append(text)
        # Update status if crawling has finished
        if "✅ Crawled:" in text or "❌" in text:
            self.status_label.setText("Status: Idle")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    ex = CrawlerApp()
    ex.show()
    sys.exit(app.exec())
