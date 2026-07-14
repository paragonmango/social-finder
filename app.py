import json
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request

app = Flask(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; SocialFinder/1.0; +https://example.com/bot)"
TIMEOUT = 8

# Map of platform name -> regex matching that platform's profile URLs
SOCIAL_PATTERNS = {
    "X / Twitter": re.compile(r"https?://(?:www\.)?(?:twitter|x)\.com/(?!intent|share|hashtag)([A-Za-z0-9_]{1,30})/?", re.I),
    "Instagram": re.compile(r"https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{1,30})/?", re.I),
    "Facebook": re.compile(r"https?://(?:www\.)?facebook\.com/([A-Za-z0-9_.\-]{1,60})/?", re.I),
    "LinkedIn": re.compile(r"https?://(?:www\.)?linkedin\.com/(?:company|in)/([A-Za-z0-9_\-]{1,80})/?", re.I),
    "YouTube": re.compile(r"https?://(?:www\.)?youtube\.com/(?:c/|channel/|@)?([A-Za-z0-9_\-]{1,80})/?", re.I),
    "TikTok": re.compile(r"https?://(?:www\.)?tiktok\.com/@([A-Za-z0-9_.]{1,30})/?", re.I),
    "GitHub": re.compile(r"https?://(?:www\.)?github\.com/([A-Za-z0-9_\-]{1,39})/?", re.I),
    "Threads": re.compile(r"https?://(?:www\.)?threads\.net/@([A-Za-z0-9_.]{1,30})/?", re.I),
    "Pinterest": re.compile(r"https?://(?:www\.)?pinterest\.com/([A-Za-z0-9_\-]{1,60})/?", re.I),
    "Reddit": re.compile(r"https?://(?:www\.)?reddit\.com/(?:u|user)/([A-Za-z0-9_\-]{1,30})/?", re.I),
    "Discord": re.compile(r"https?://(?:www\.)?discord\.(?:gg|com/invite)/([A-Za-z0-9_\-]{1,40})/?", re.I),
}

CANDIDATE_PATHS = ["", "/about", "/about-us", "/contact", "/contact-us"]

# Generic path segments that look like a "profile handle" to the regexes
# above but are actually nav/marketing pages, not a person's/brand's profile.
NON_PROFILE_SEGMENTS = {
    "login", "signup", "signin", "join", "about", "about-us", "contact",
    "contact-us", "features", "pricing", "enterprise", "security",
    "solutions", "resources", "customer-stories", "open-source", "topics",
    "trending", "sponsors", "team", "marketplace", "newsletter", "newsroom",
    "logos", "sitemap", "social-impact", "trust-center", "mobile", "edu",
    "git-guides", "readme", "collections", "orgs", "partners", "help",
    "jobs", "careers", "terms", "privacy", "policies", "developers",
    "business", "ads", "support", "watch", "results", "shorts", "feed",
    "explore", "settings", "notifications", "messages", "search",
    "hashtag", "intent", "share", "sharer", "dialog", "plugins", "tr",
    "pages", "groups", "company", "school", "showcase", "stars", "home",
    "index", "app", "download", "apps", "legal", "cookies", "accounts",
    "channel", "playlist", "premium", "music", "gaming", "kids",
}


def is_real_handle(handle: str) -> bool:
    return handle.lower() not in NON_PROFILE_SEGMENTS


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    return raw


def fetch(url: str):
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        if resp.status_code >= 400:
            return None
        return resp.text
    except requests.RequestException:
        return None


SOCIAL_CONTAINER_RE = re.compile(r"social|follow-us|footer|site-footer", re.I)


def extract_links(html: str, base_url: str) -> tuple:
    """Returns (priority_links, all_links). Priority links come from the
    page header/footer/nav/social-icon areas and site metadata — the places
    a site's own official social buttons normally live, as opposed to links
    embedded in body content (e.g. a contributor's personal profile)."""
    soup = BeautifulSoup(html, "html.parser")
    all_links = set()
    priority_links = set()

    priority_containers = soup.find_all(["header", "footer", "nav"])
    priority_containers += soup.find_all(
        lambda tag: tag.has_attr("class") and SOCIAL_CONTAINER_RE.search(" ".join(tag["class"]))
    )
    priority_containers += soup.find_all(
        lambda tag: tag.has_attr("id") and SOCIAL_CONTAINER_RE.search(tag["id"])
    )

    for container in priority_containers:
        for a in container.find_all("a", href=True):
            priority_links.add(urljoin(base_url, a["href"]))

    for a in soup.find_all("a", href=True):
        all_links.add(urljoin(base_url, a["href"]))

    for meta in soup.find_all("meta", content=True):
        name = (meta.get("name") or meta.get("property") or "").lower()
        if "twitter" in name or "og:see_also" in name:
            priority_links.add(meta["content"])
            all_links.add(meta["content"])

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            same_as = item.get("sameAs")
            same_as_links = [same_as] if isinstance(same_as, str) else same_as if isinstance(same_as, list) else []
            priority_links.update(same_as_links)
            all_links.update(same_as_links)

    return priority_links, all_links


def classify(links: set) -> dict:
    found = {}
    for link in links:
        for platform, pattern in SOCIAL_PATTERNS.items():
            m = pattern.match(link)
            if m and is_real_handle(m.group(1)):
                found.setdefault(platform, set()).add(m.group(0))
    return {platform: sorted(urls) for platform, urls in found.items()}


def find_socials(start_url: str) -> dict:
    parsed = urlparse(start_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    priority_links = set()
    all_links = set()
    pages_checked = []

    candidate_urls = [urljoin(origin, path) for path in CANDIDATE_PATHS]
    with ThreadPoolExecutor(max_workers=len(candidate_urls)) as pool:
        htmls = pool.map(fetch, candidate_urls)

    for page_url, html in zip(candidate_urls, htmls):
        if html is None:
            continue
        pages_checked.append(page_url)
        page_priority, page_all = extract_links(html, page_url)
        priority_links |= page_priority
        all_links |= page_all

    results = classify(priority_links)
    if not results:
        results = classify(all_links)

    return {
        "pages_checked": pages_checked,
        "results": results,
    }


@app.route("/", methods=["GET", "POST"])
def index():
    results = None
    pages_checked = None
    error = None
    submitted_url = ""

    if request.method == "POST":
        submitted_url = request.form.get("url", "")
        if not submitted_url.strip():
            error = "Enter a URL first."
        else:
            target = normalize_url(submitted_url)
            if not fetch(target):
                error = f"Couldn't reach {target}. Check the URL and try again."
            else:
                data = find_socials(target)
                results = data["results"]
                pages_checked = data["pages_checked"]
                if not results:
                    error = "No social links found on the homepage/about/contact pages."

    return render_template(
        "index.html",
        results=results,
        pages_checked=pages_checked,
        error=error,
        submitted_url=submitted_url,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
