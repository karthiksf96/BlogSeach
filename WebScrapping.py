from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, Dict, List
import requests
from bs4 import BeautifulSoup
import difflib
from urllib.parse import urljoin, urlparse
import concurrent.futures
import os # Keep os for abspath if you still want to generate local files for debug on the server, but it won't be accessible by client.

app = FastAPI(title="Blog Search API")

# Allow CORS for external access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Be more specific in production, e.g., ["https://your-salesforce-domain.my.salesforce.com"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global config
BASE_URL = "https://d5meta.com" # Removed the trailing space here!
MAIN_SITEMAP = urljoin(BASE_URL, "/wp-sitemap.xml")
HEADERS = {"User-Agent": "Mozilla/5.0"}

# Pydantic model for the request body
class SearchRequest(BaseModel):
    query: str

# Pydantic model for the response body
class BlogSearchResult(BaseModel):
    title: Optional[str]
    url: Optional[str]
    content_preview: Optional[str]
    full_content: Optional[str]
    image_urls: List[str] = [] # Added image_urls to the response
    message: Optional[str] = None # For "No matching blog found." scenarios
    error: Optional[str] = None # For explicit error messages

@app.post("/search-blog", response_model=BlogSearchResult)
async def search_blog_endpoint(request: SearchRequest): # Changed to async for potential async operations if needed
    search_query = request.query
    try:
        # Call the core logic function
        result = run_blog_search(search_query)

        # Check for errors/messages from run_blog_search
        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])
        if "message" in result:
            return BlogSearchResult(message=result["message"])

        return BlogSearchResult(**result) # Return the Pydantic model instance
    except HTTPException as he:
        raise he # Re-raise FastAPI's HTTPExceptions
    except Exception as e:
        print(f"An unexpected error occurred: {e}") # Log the full error
        raise HTTPException(status_code=500, detail=f"An internal server error occurred: {str(e)}")


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


def get_sitemap_urls() -> List[str]:
    try:
        res = requests.get(MAIN_SITEMAP, headers=HEADERS, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "xml")
        return [loc.text for loc in soup.find_all("loc")]
    except Exception as e:
        print(f"❌ Sitemap fetch error: {e}")
        return []


def get_all_blog_page_urls() -> List[str]:
    urls = []
    # Use ThreadPoolExecutor here as well for fetching sitemap URLs concurrently
    sitemap_urls = get_sitemap_urls()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor: # Smaller pool for sitemaps
        future_to_url = {executor.submit(fetch_sitemap_content, sitemap_url): sitemap_url for sitemap_url in sitemap_urls}
        for future in concurrent.futures.as_completed(future_to_url):
            sitemap_content = future.result()
            if sitemap_content:
                soup = BeautifulSoup(sitemap_content, "xml")
                urls.extend([
                    loc.text for loc in soup.find_all("loc")
                    if any(keyword in loc.text.lower() for keyword in ["blog", "signature", "salesforce", "field", "service", "dispatcher"])
                ])
    return urls

def fetch_sitemap_content(sitemap_url: str) -> Optional[bytes]:
    try:
        res = requests.get(sitemap_url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        return res.content
    except Exception as e:
        print(f"Error fetching sitemap {sitemap_url}: {e}")
        return None


def get_title_from_url(url: str) -> Optional[tuple[str, str]]:
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "html.parser")
        title_tag = soup.find("h1") or soup.find("title")
        if title_tag:
            return (title_tag.get_text(strip=True), url)
    except Exception as e: # Catch specific exception for better debugging
        print(f"Error getting title from {url}: {e}")
    return None


def fetch_blog_soup(url: str) -> Optional[BeautifulSoup]:
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        soup = BeautifulSoup(res.text, "html.parser")
        return soup
    except Exception as e:
        print(f"Error fetching blog soup from {url}: {e}")
        return None


def extract_blog_content(soup: BeautifulSoup) -> Optional[str]:
    main_content = soup.select_one("div.elementor-widget-container") or soup.body
    if not main_content:
        return None
    content_blocks = main_content.find_all(["p", "h2", "h3", "li"])
    content = "\n".join(clean_html(str(b)) for b in content_blocks if b.get_text(strip=True))
    return content.strip()


def slugify(text: str) -> str:
    return text.lower().replace(" ", "-")


# This is the core logic, separated from the API endpoint
def run_blog_search(search_query: str) -> Dict[str, any]:
    print(f"⏳ Searching for blog: {search_query}") # Will appear in server logs

    urls = get_all_blog_page_urls()
    if not urls:
        return {"error": "No blog URLs found from sitemap."}

    print(f"🔎 Checking {len(urls)} URLs...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(get_title_from_url, urls))

    title_url_map = {}
    for result in results:
        if result:
            title, url = result
            title_url_map[title] = url

    query_slug = slugify(search_query)
    # Ensure URL parsing is robust, avoid errors for invalid URLs
    slug_url_map = {}
    for url in urls:
        try:
            path_parts = urlparse(url).path.split("/")
            if len(path_parts) >= 2: # Ensure there's at least a potential slug part
                slug = path_parts[-2] if path_parts[-1] == "" else path_parts[-1] # Handle trailing slashes
                if "-" in slug:
                    slug_url_map[slug] = url
        except Exception as e:
            print(f"Warning: Could not parse URL for slug: {url} - {e}")


    slugs = list(slug_url_map.keys())
    close_slugs = difflib.get_close_matches(query_slug, slugs, n=1, cutoff=0.5)

    match_title = None
    match_url = None

    if close_slugs:
        best_slug = close_slugs[0]
        match_url = slug_url_map[best_slug]
        title_result = get_title_from_url(match_url)
        match_title = title_result[0] if title_result else f"Matched via URL: {best_slug}"
    else:
        titles = list(title_url_map.keys())
        best_match = difflib.get_close_matches(search_query, titles, n=1, cutoff=0.6)
        if not best_match:
            return {"message": "No matching blog found."} # Return a specific message instead of error

        match_title = best_match[0]
        match_url = title_url_map[match_title]

    print(f"✅ Closest match: {match_title}")
    print(f"🔗 URL: {match_url}")

    soup = fetch_blog_soup(match_url)
    if not soup:
        return {"error": f"Failed to fetch blog HTML for {match_url}."}

    content = extract_blog_content(soup)
    if not content:
        return {"error": "Failed to extract blog content."}

    # Extract image URLs
    images = soup.select("img")
    image_urls = []
    for img in images[:3]: # Get up to 3 images
        src = img.get("src")
        if src:
            full_src = urljoin(match_url, src)
            image_urls.append(full_src)

    # Do NOT call render_rich_blog_output here if you want JSON output
    # render_rich_blog_output(match_title, match_url, content, soup) # This will create a file on the server.

    return {
        "title": match_title,
        "url": match_url,
        "content_preview": content[:300], # Provide a short preview
        "full_content": content,         # Provide full content
        "image_urls": image_urls         # Provide image URLs
    }

# This section is only for local development via `uvicorn main:app --reload`
# It's not part of the deployed API logic per se.
if __name__ == "__main__":
    import uvicorn
    # To run locally: uvicorn your_script_name:app --reload
    # This will typically run on http://127.0.0.1:8000
    uvicorn.run(app, host="0.0.0.0", port=8000)