Advanced Web Crawler
====================
Requirements:
-------------
PyQt6==6.5.0        
selenium==4.11.0     
webdriver_manager==3.8.6
requests==2.31.0     
Overview:
---------
This project is an advanced web crawler built with Python, PyQt6, and Selenium. It extracts various data from a given URL (such as links, images, text, metadata, and raw HTML) and stores the data in an organized folder structure. The crawler also provides a GUI for controlling the crawl (including options for extraction, depth, and output folder) as well as a stop button and progress bar for real-time feedback.

Folder Structure:
-----------------
The project is organized as follows:

AdvancedWebCrawler/
├── advanced_web_crawler.py   - The main source code file.
├── requirements.txt          - Dependency management file.
└── README.txt                - Setup and usage instructions.

When the crawler runs, it creates an output folder (default is "output" unless changed via the GUI) where data for each crawled page is stored as follows:
  
  output/
      domain_name/           (e.g., books_toscrape_com)
          safe_hash/         (a unique folder for each page)
              title.txt      → Contains the page title
              links.txt      → Contains extracted links (one per line)
              images.txt     → Contains downloaded image file names
              text.txt       → Contains extracted text content
              metadata.json  → Contains extracted metadata in JSON format
              source.html    → Contains the raw HTML source (if enabled)
              crawl_log.txt  → Contains a summary log for the page
              images/        → Folder with downloaded image files

System Requirements:
--------------------
- OS: Windows, macOS, or Linux
- Python 3.8 or later (tested on Python 3.12)
- The following Python libraries (listed in requirements.txt)

Setup Instructions:
-------------------
1. **Clone or Download the Project:**
   - Place all the files in a folder named, for example, `Crawler`.

2. **Set Up a Virtual Environment (optional but recommended):**
   ```bash
   python3 -m venv venv
   source venv/bin/activate    # On Windows: venv\\Scripts\\activate
