import httpx
from typing import Optional
from app.net_safety import safe_get
from app.schemas import NewsItem

class ContentFetcher:
    """Full-text fetcher with 4-step cascade for paywalled content.
    
    Cascade: trafilatura → archive.ph (8s hard timeout) → Google News alt → graceful degradation
    v3: archive.ph has anti-crawl fast-fail (403/429/cf-browser-verification)
    """
    
    def __init__(self, proxy: Optional[str] = None,
                 archive_ph_timeout: int = 8,
                 source_timeout: int = 20,
                 cloudflare_markers: Optional[list[str]] = None):
        self.proxy = proxy
        self.archive_ph_timeout = archive_ph_timeout
        self.source_timeout = source_timeout
        self.cloudflare_markers = cloudflare_markers or [
            "cf-browser-verification", "cf-challenge", "just a moment"
        ]
    
    def fetch_full_text(self, item: NewsItem) -> tuple[Optional[str], str]:
        """Fetch full article text using 4-step cascade.
        
        Returns (content, content_source):
        - content: Article text or None if all methods fail
        - content_source: 'full_text'|'archive_ph'|'google_news_alt'|'rss_summary'
        """
        
        # Step 1: Try trafilatura on original URL
        content = self._fetch_via_trafilatura(item.url)
        if content and len(content) >= 200:
            return content, "full_text"
        
        # Step 2: archive.ph (only for paywalled/major news)
        if item.is_paywalled or (item.classification and item.classification.is_major):
            content = self._fetch_via_archive_ph(item.url)
            if content and len(content) >= 200:
                return content, "archive_ph"
        
        # Step 3: Google News alternative source
        content = self._fetch_via_google_news_alt(item.title)
        if content and len(content) >= 200:
            return content, "google_news_alt"
        
        # Step 4: Graceful degradation - use summary
        return None, "rss_summary"
    
    def _fetch_via_trafilatura(self, url: str) -> Optional[str]:
        """Fetch using trafilatura for clean text extraction."""
        try:
            import trafilatura
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            
            response = safe_get(
                url,
                timeout=self.source_timeout,
                proxy=self.proxy,
                headers=headers,
            )
            response.raise_for_status()
            
            content = trafilatura.extract(response.text)
            return content
        except Exception:
            return None
    
    def _fetch_via_archive_ph(self, url: str) -> Optional[str]:
        """v3: Fetch via archive.ph with 8s hard timeout + anti-crawl fast-fail.
        
        CRITICAL: 
        - 8 second hard timeout (configurable)
        - HTTP 403/429 → immediate return None, NO retry
        - Response contains cf-browser-verification → immediate return None, NO retry
        - NEVER use tenacity retry on this method
        """
        try:
            archive_url = f"https://archive.ph/newest/{url}"
            
            response = safe_get(
                archive_url,
                timeout=self.archive_ph_timeout,
                proxy=self.proxy,
            )
            
            # v3: Anti-crawl fast-fail detection
            if response.status_code in (403, 429):
                return None
            
            # Check for Cloudflare challenge markers
            body = response.text.lower()
            for marker in self.cloudflare_markers:
                if marker in body:
                    return None
            
            response.raise_for_status()
            
            # Extract text from archived page
            import trafilatura
            content = trafilatura.extract(response.text)
            return content
            
        except (httpx.TimeoutException, Exception):
            # Timeout or any other error → immediate fail, no retry
            return None
    
    def _fetch_via_google_news_alt(self, title: str) -> Optional[str]:
        """Find alternative open-source article via Google News search."""
        try:
            import trafilatura
            from urllib.parse import quote_plus
            
            # Search Google News for same topic
            query = quote_plus(title)
            search_url = f"https://news.google.com/rss/search?q={query}"
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            }
            
            response = safe_get(
                search_url,
                timeout=self.source_timeout,
                proxy=self.proxy,
                headers=headers,
            )
            response.raise_for_status()
            
            # Parse RSS to get first non-paywalled result
            import feedparser
            feed = feedparser.parse(response.text)
            
            for entry in feed.entries[:5]:  # Check first 5 results
                link = entry.get("link", "")
                if not link:
                    continue
                
                # Skip known paywalled domains
                paywalled_domains = ["wsj.com", "ft.com", "bloomberg.com", "nytimes.com"]
                if any(domain in link.lower() for domain in paywalled_domains):
                    continue
                
                # Try to fetch this alternative
                content = self._fetch_via_trafilatura(link)
                if content and len(content) >= 200:
                    return content
            
            return None
        except Exception:
            return None
