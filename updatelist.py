import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin
from urllib.robotparser import RobotFileParser
import threading
import time
from queue import Queue, Empty
import os
from tqdm import tqdm
import json
import stem
from stem import Signal
from stem.control import Controller
from fake_useragent import UserAgent
import re
import subprocess
import random
import sys

# --- Configuration ---
SEED_FILE = "seed.txt"
WEBPAGES_FILE = "WEBPAGES.TXT"
USER_AGENT = "SearchPlus-Crawler_by_Scratch2033 " + UserAgent().random # For spoofing Cloudflare CAPTCHAs
MAX_THREADS_PER_DOMAIN = 5
MAX_GLOBAL_CRAWL_THREADS = 10
MAX_RETRIES = 3 # Added for make_request function
def count_newlines(filepath):
    """Counts newlines in a file by reading the entire content."""
    try:
        with open(filepath, 'r') as file:
            content = file.read()
            return content.count('\n')
    except FileNotFoundError:
        return "Error: File not found."
    except Exception as e:
        return f"An error occurred: {e}"
TARGET_WEBSITE_LIMIT = count_newlines("seed.txt") + 2000
# Output control
CRAWLED_PRINT_INTERVAL = 5 # Print crawled count every 5 pages
CLEAR_OUTPUT_INTERVAL = 25 # Clear output every 25 crawled pages

# TOR Configuration
TOR_PROXY = {
    'http': 'socks5h://127.0.0.1:9050',
    'https': 'socks5h://127.0.0.1:9050'
}
TOR_CONTROL_PORT = 5090
TOR_CONTROL_PASSWORD = None  # Set if you have a password

# --- Global Data Structures (Thread-Safe) ---
seed_websites = set()
seed_lock = threading.Lock()
crawled_urls = set()
crawled_urls_lock = threading.Lock()
webpages_to_save_queue = Queue()
robots_parsers = {}
robots_lock = threading.Lock()
tqdm_bar = None
tqdm_lock = threading.Lock()
stop_event = threading.Event()
domain_queue_for_main = None
crawled_page_count = 0
crawled_page_count_lock = threading.Lock()


# --- TOR Helper Functions ---
def process_page(url, html_content):
    """Extract and store data from a page"""
    soup = BeautifulSoup(html_content, 'html.parser')

    # Store raw HTML
    os.makedirs('pages', exist_ok=True) # Create 'pages' directory if it doesn't exist
    with open(f"pages/{hash(url)}.html", "w", encoding='utf-8') as f:
        f.write(html_content)

    # Extract and store links using the new string-based method
    links = []
    for match in re.finditer(r'(https?://[^\s"\'>]+)', html_content):
        extracted_url = match.group(1)
        normalized = normalize_url(url, extracted_url)
        if normalized:
            links.append(normalized)


    with open("links.txt", "a") as f:
        f.write("\n".join(links) + "\n")

    return links

def get_tor_session():
    """Create a requests session that uses TOR with YOUR exact user agent"""
    session = requests.Session()
    session.proxies = TOR_PROXY
    session.headers.update({
        'User-Agent': USER_AGENT,  # Will use your exact string
        'Accept-Language': 'en-US,en;q=0.5',
        'Connection': 'keep-alive',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8'
    })
    return session

def renew_tor_connection():
    """Change TOR exit node to get a new IP"""
    try:
        with Controller.from_port(port=TOR_CONTROL_PORT) as controller:
            if TOR_CONTROL_PASSWORD:
                controller.authenticate(password=TOR_CONTROL_PASSWORD)
            else:
                controller.authenticate()
            controller.signal(Signal.NEWNYM)
            print("TOR IP changed successfully", file=sys.stderr) # Print to stderr to keep it separate from main output
    except Exception as e:
        print(f"Error changing TOR IP: {e}", file=sys.stderr) # Print to stderr

def add_scheme_if_missing(url):
    """Adds http:// scheme to a URL if it's missing."""
    if not urlparse(url).scheme:
        return "http://" + url
    return url

def make_tor_request(url):
    session = get_tor_session()
    url_with_scheme = add_scheme_if_missing(url) # Add scheme here
    try:
        response = session.get(url_with_scheme, timeout=30)

        if response.status_code == 429:  # Rate limited
            renew_tor_connection()
            time.sleep(30)
            return None

        return response

    except Exception as e:
        print(f"Request failed: {str(e)[:100]}", file=sys.stderr) # Print to stderr
        renew_tor_connection()
        return None

# --- Modified Helper Functions ---

def get_root_domain(url):
    """Extracts the root domain from a given URL using the new string-based method."""
    try:
        url = add_scheme_if_missing(url).lower() # Add scheme here before parsing
        if url.startswith("http://"):
            url = url[len("http://"):]
        elif url.startswith("https://"):
            url = url[len("https://"):]

        slash_index = url.find('/')
        if slash_index != -1:
            domain = url[:slash_index]
        else:
            domain = url

        # Remove port number if present
        if ':' in domain:
            domain = domain.split(':')[0]

        # Simple check for valid characters in domain
        if re.match(r"^[a-z0-9.-]+$", domain):
             return domain
        else:
            return None

    except Exception as e:
        print(f"Error parsing URL {url}: {e}", file=sys.stderr) # Print to stderr
    return None


def normalize_url(base_url, relative_url):
    """Normalizes a URL by resolving relative paths and ensuring it's absolute."""
    try:
        absolute_url = urljoin(base_url, relative_url)
        parsed = urlparse(absolute_url)
        normalized = parsed._replace(fragment="", query="").geturl()
        return normalized
    except Exception as e:
        print(f"Error normalizing URL {relative_url} from base {base_url}: {e}", file=sys.stderr) # Print to stderr
    return None

def get_robot_parser(domain):
    """Fetches and caches the robots.txt parser for a given domain using TOR."""
    with robots_lock:
        if domain not in robots_parsers:
            rp = RobotFileParser()
            robots_urls_to_try = [f"https://{domain}/robots.txt", f"http://{domain}/robots.txt"]
            found_robots = False

            for robots_url in robots_urls_to_try:
                response = make_tor_request(robots_url)
                if response and response.status_code == 200:
                    rp.parse(response.text.splitlines())
                    found_robots = True
                    # print(f"[{domain}] Successfully fetched robots.txt from {robots_url}", file=sys.stderr) # Suppress this output
                    break
                else:
                    # print(f"[{domain}] No robots.txt found or error fetching from {robots_url}", file=sys.stderr) # Suppress this output
                    pass

            if not found_robots:
                # print(f"[{domain}] Could not fetch robots.txt from any known URL. Assuming allowed.", file=sys.stderr) # Suppress this output
                pass
            robots_parsers[domain] = rp
        return robots_parsers[domain]

def can_fetch(url, user_agent):
    """Checks if the given URL can be fetched according to robots.txt."""
    domain = get_root_domain(url)
    if not domain:
        return False
    rp = get_robot_parser(domain)
    if rp is None:
        return True
    return rp.can_fetch(user_agent, url)

def add_new_seed(new_domain):
    """Adds a new unique root domain to the global seed_websites set and seed.txt."""
    global domain_queue_for_main
    with seed_lock:
        if new_domain not in seed_websites:
            if len(seed_websites) < TARGET_WEBSITES_LIMIT:
                seed_websites.add(new_domain)
                with open(SEED_FILE, 'a') as f:
                    f.write(new_domain + '\n')
                # print(f"New website added to seed.txt: {new_domain}", file=sys.stderr) # Suppress this output

                with tqdm_lock:
                    if tqdm_bar:
                        tqdm_bar.update(1)

                if domain_queue_for_main:
                    new_domain_initial_url = f"https://{new_domain}/"
                    domain_queue_for_main.put(new_domain_initial_url)
                    # print(f"Added {new_domain_initial_url} to main crawling queue.", file=sys.stderr) # Suppress this output
                return True
            else:
                print(f"Target website limit ({TARGET_WEBSITES_LIMIT}) reached. Not adding {new_domain}.", file=sys.stderr) # Print to stderr
                return False
        return False

def webpage_writer_thread():
    """Dedicated thread to write discovered webpages to WEBPAGES.TXT."""
    # print("Webpage writer thread started.", file=sys.stderr) # Suppress this output
    webpage_print_counter = 0
    while not stop_event.is_set() or not webpages_to_save_queue.empty():
        try:
            url_to_save = webpages_to_save_queue.get(timeout=1)
            with open(WEBPAGES_FILE, 'a') as f:
                f.write(url_to_save + '\n')

            global crawled_page_count
            with crawled_page_count_lock:
                crawled_page_count += 1

            # webpage_print_counter += 1
            # if webpage_print_counter % 5 == 0:
            #     print(f"Webpage added: {url_to_save}", file=sys.stderr) # Suppress this output

            webpages_to_save_queue.task_done()
        except Empty:
            continue
        except Exception as e:
            print(f"Error writing to WEBPAGES.TXT: {e}", file=sys.stderr) # Print to stderr
    # print("Webpage writer thread stopped.", file=sys.stderr) # Suppress this output

def crawl_domain(initial_url_for_domain):
    """Crawls a single root domain using TOR with IP rotation."""
    domain = get_root_domain(initial_url_for_domain)
    if not domain:
        print(f"Skipping invalid initial URL: {initial_url_for_domain}", file=sys.stderr) # Print to stderr
        return

    # print(f"Starting crawl for domain: {domain}", file=sys.stderr) # Suppress this output
    domain_internal_queue = Queue()
    domain_internal_queue.put(initial_url_for_domain)
    visited_urls_in_domain = set()

    while not stop_event.is_set() and not domain_internal_queue.empty():
        current_url = None
        try:
            current_url = domain_internal_queue.get(timeout=1)

            with crawled_urls_lock:
                if current_url in visited_urls_in_domain or current_url in crawled_urls:
                    domain_internal_queue.task_done()
                    continue
                crawled_urls.add(current_url)

            visited_urls_in_domain.add(current_url)

            if not can_fetch(current_url, USER_AGENT):
                domain_internal_queue.task_done()
                continue

            # print(f"[{domain}] Fetching: {current_url}", file=sys.stderr) # Suppress this output
            response = make_tor_request(current_url)

            if not response:
                domain_internal_queue.task_done()
                continue

            webpages_to_save_queue.put(current_url)

            links = process_page(current_url, response.text)

            for new_url in links:
                new_domain = get_root_domain(new_url)

                if new_domain == domain:
                    if new_url not in visited_urls_in_domain:
                        domain_internal_queue.put(new_url)
                elif new_domain and new_domain != domain:
                    if not stop_event.is_set():
                        add_new_seed(new_domain)

            domain_internal_queue.task_done()
            time.sleep(1)  # Be polite with delay between requests
            renew_tor_connection()  # Change IP after each request

        except requests.exceptions.RequestException as e:
            if current_url:
                with crawled_urls_lock:
                    crawled_urls.discard(current_url)
                domain_internal_queue.task_done()
            if not stop_event.is_set():
                print(f"[{domain}] Request error for {current_url}: {e}", file=sys.stderr) # Print to stderr
        except Exception as e:
            if current_url:
                domain_internal_queue.task_done()
            if not stop_event.is_set():
                print(f"[{domain}] Unexpected error for {current_url}: {e}", file=sys.stderr) # Print to stderr

    # print(f"Finished crawling domain: {domain}", file=sys.stderr) # Suppress this output

def enable_tor():
    """Force enable TOR and disable WARP"""
    subprocess.run(["sudo", "systemctl", "start", "tor"], check=True)
    subprocess.run(["/usr/bin/warp-cli", "disconnect"], stdout=subprocess.DEVNULL)

def enable_warp():
    """Force enable WARP and disable TOR"""
    subprocess.run(["/usr/bin/warp-cli", "connect"], stdout=subprocess.DEVNULL)
    subprocess.run(["sudo", "systemctl", "stop", "tor"], check=True)

def is_same_domain(url1, url2):
    """Check if two URLs share the same root domain"""
    return get_root_domain(url1) == get_root_domain(url2)

def make_request(url, timeout=30):
    """Make request with automatic TOR/WARP failover"""
    url_with_scheme = add_scheme_if_missing(url) # Add scheme here
    for attempt in range(MAX_RETRIES):
        # Alternate between TOR and WARP
        if attempt % 2 == 0:
            enable_tor()
            proxies = {'http': 'socks5h://127.0.0.1:9050',
                      'https': 'socks5h://127.0.0.1:9050'}
        else:
            enable_warp()
            proxies = None

        try:
            response = requests.get(
                url_with_scheme, # Use URL with scheme here
                headers={'User-Agent': USER_AGENT},
                proxies=proxies,
                timeout=timeout
            )

            # Detect blocking
            if response.status_code in [403, 429, 503] or "captcha" in response.text.lower():
                raise requests.exceptions.RequestException(f"Block detected (Status {response.status_code})")

            return response

        except Exception as e:
            print(f"Attempt {attempt+1} failed: {str(e)[:100]}", file=sys.stderr) # Print to stderr
            time.sleep(random.uniform(5, 10))

    return None


# --- Main Execution ---
def main():
    global tqdm_bar, domain_queue_for_main, USER_AGENT, crawled_page_count

    try:
        os.makedirs('/content/crawlergit', exist_ok=True)
        os.chdir('/content/crawlergit')
        print("Changed working directory to /content/crawlergit")
    except OSError as e:
        print(f"Error changing directory to /content/crawlergit: {e}")


    # Initialize files
    if not os.path.exists(SEED_FILE):
        with open(SEED_FILE, 'w') as f:
            f.write("https://www.google.com/\n")
        print(f"'{SEED_FILE}' created with a default google URL.")

    if not os.path.exists(WEBPAGES_FILE):
        open(WEBPAGES_FILE, 'w').close()

    # Load initial seeds
    initial_seed_urls_from_file = []
    with open(SEED_FILE, 'r') as f:
        for line in f:
            url = line.strip()
            if url:
                domain = get_root_domain(url)
                if domain and len(seed_websites) < TARGET_WEBSITES_LIMIT:
                    seed_websites.add(domain)
                    initial_seed_urls_from_file.append(url)
                elif len(seed_websites) >= TARGET_WEBSITES_LIMIT:
                    print(f"Initial seed limit reached ({TARGET_WEBSITES_LIMIT}). Skipping further seeds from file.", file=sys.stderr) # Print to stderr
                    break

    if not seed_websites:
        print("No seed websites found in seed.txt. Please add URLs to seed.txt to start crawling.", file=sys.stderr) # Print to stderr
        return

    print(f"Starting crawler with {len(seed_websites)} initial unique seed domains.")

    print(f"Using User-Agent: {USER_AGENT}")

    # Start writer thread
    writer_thread = threading.Thread(target=webpage_writer_thread, daemon=True)
    writer_thread.start()

    # Setup progress bar
    with tqdm_lock:
        tqdm_bar = tqdm(total=TARGET_WEBSITES_LIMIT,
                        desc="Crawling Progress",
                        unit="domain",
                        initial=len(seed_websites),
                        file=sys.stdout) # Direct tqdm output to stdout

    # Initialize main queue
    domain_queue_for_main = Queue()
    for url in initial_seed_urls_from_file:
        domain_queue_for_main.put(url)

    # Start thread pool
    thread_pool_executor = threading.Thread(target=lambda: _run_domain_crawler_pool(domain_queue_for_main), daemon=True)
    thread_pool_executor.start()

    try:
        while not stop_event.is_set():
            current_url = domain_queue_for_main.get()
            domain = get_root_domain(current_url)

            if not domain:
                domain_queue_for_main.task_done()
                continue

            # print(f"Processing initial URL for domain: {domain} - {current_url}", file=sys.stderr) # Suppress this output

            response = make_tor_request(current_url)
            if not response:
                domain_queue_for_main.task_done()
                continue

            webpages_to_save_queue.put(current_url)

            links = process_page(current_url, response.text)

            for new_url in links:
                new_domain = get_root_domain(new_url)

                if new_domain == domain:
                    if new_url not in visited_urls_in_domain:
                        domain_queue_for_main.put(new_url)
                elif new_domain and new_domain != domain:
                    if not stop_event.is_set():
                        add_new_seed(new_domain)


            domain_queue_for_main.task_done()
            time.sleep(5)  # Be polite
            renew_tor_connection()  # Rotate IP after each page

            # Clear output and print updated info periodically
            with crawled_page_count_lock:
                if crawled_page_count % CLEAR_OUTPUT_INTERVAL == 0:
                    clear_output(wait=True)
                    with tqdm_lock:
                        tqdm_bar.refresh() # Refresh tqdm to keep it visible
                    print(f"Crawled: {crawled_page_count} webpages") # Print crawled count

    except KeyboardInterrupt:
        print("\nCrawler interrupted by user. Shutting down...", file=sys.stderr) # Print to stderr
    finally:
        stop_event.set()
        if tqdm_bar:
            tqdm_bar.close()
        print("Waiting for threads to finish...", file=sys.stderr) # Print to stderr
        webpages_to_save_queue.join()
        domain_queue_for_main.join()
        if thread_pool_executor:
            thread_pool_executor.join(timeout=60)
        writer_thread.join(timeout=60)
        print("Crawler finished.", file=sys.stderr) # Print to stderr

def _run_domain_crawler_pool(domain_queue):
    """Manages the pool of threads for crawling individual domains."""
    semaphore = threading.Semaphore(MAX_GLOBAL_CRAWL_THREADS)
    while not stop_event.is_set():
        try:
            domain_url = domain_queue.get(timeout=2)
            domain = get_root_domain(domain_url)
            if not domain:
                domain_queue.task_done()
                continue

            semaphore.acquire()
            thread = threading.Thread(
                target=lambda url=domain_url, sem=semaphore, main_q=domain_queue: _crawl_domain_wrapper(url, sem, main_q),
                daemon=True
            )
            thread.start()

        except Empty:
            with seed_lock:
                if len(seed_websites) >= TARGET_WEBSITES_LIMIT:
                    stop_event.set()
            time.sleep(5)
        except Exception as e:
                print(f"Error in domain crawler pool manager: {e}", file=sys.stderr) # Print to stderr
                time.sleep(1)

def _crawl_domain_wrapper(initial_url, semaphore, main_q):
    """Wrapper for crawl_domain to ensure semaphore is released."""
    try:
        crawl_domain(initial_url)
    finally:
        semaphore.release()
        main_q.task_done()


main()
import os
import re

# Path to webpages.txt
file_path = "WEBPAGES.TXT"

# File extensions to remove (images, docs, archives, etc.)
file_extensions = [
    '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.webp',
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.mp3', '.mp4', '.avi', '.mov', '.wav',
    '.zip', '.rar', '.tar', '.gz', '.7z',
    '.exe', '.dll', '.deb', '.rpm', '.apk', '.ogv', '.ppd', '.ogg', '.dtd'
]

# Domains to exclude (e.g., Google Play links)
excluded_domains = [
    'play.google.com',
    'embed.youtube.com',
]

def clean_url(url):
    """Remove unwanted characters and fix malformed URLs."""
    # Remove trailing HTML tags, weird symbols, etc.
    url = re.sub(r'[<\s].*$', '', url.strip())
    
    # Remove common tracking parameters (UTM, etc.)
    url = re.sub(r'(\?|&)(utm_[^&]+|fbclid|gclid)=[^&]*', '', url)
    
    return url

def is_valid_web_url(url):
    """Check if URL is valid and not a file/excluded domain."""
    # Must start with http:// or https://
    if not url.startswith(('http://', 'https://')):
        return False
    
    # Must not be a file (based on extensions)
    if any(url.lower().endswith(ext) for ext in file_extensions):
        return False
    
    # Must not be from excluded domains
    domain = re.match(r'https?://([^/]+)', url.lower())
    if domain and any(excluded in domain.group(1) for excluded in excluded_domains):
        return False
    
    return True

def clean_webpages(file_path):
    if not os.path.exists(file_path):
        print("Error: webpages.txt not found at", file_path)
        return
    
    with open(file_path, 'r') as f:
        lines = f.readlines()
    
    cleaned_urls = set()  # Using a set to avoid duplicates
    
    for line in lines:
        url = clean_url(line)
        if is_valid_web_url(url):
            cleaned_urls.add(url + '\n')  # Ensure newline
    
    # Overwrite the file
    with open(file_path, 'w') as f:
        f.writelines(sorted(cleaned_urls))  # Sort for readability
    
    print(f"Original: {len(lines)} entries")
    print(f"Cleaned: {len(cleaned_urls)} valid URLs remaining")

# Run the cleaner
clean_webpages(file_path)
