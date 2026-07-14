import json
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request

app = Flask(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
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

# Templates for guessing a profile URL from a bare username, used for the
# "other accounts using this username" cross-platform check.
#
# Deliberately a curated list, not "every platform." Most social apps are
# client-rendered and return HTTP 200 for a profile page whether or not
# the username exists — the "not found" state only appears after
# JavaScript runs, which a plain HTTP request never sees. Testing
# confirmed Instagram, Reddit, Pinterest, Twitch, TikTok, Threads, Spotify,
# Snapchat, and Telegram all give false positives this way, so they're
# excluded. Every platform below was verified against both a real and a
# clearly-fake username to confirm it actually distinguishes the two
# (via a real 404/403, or "not found" text present in the plain HTML).
#
# Deliberately excluded regardless of technical feasibility: adult-content
# platforms (e.g. OnlyFans). A username-in/account-out lookup for those is
# a doxxing/outing vector — the person being looked up is very often the
# one who doesn't want that link made, unlike a GitHub or Behance profile.
#
# X/Twitter was tested and cut too: it only distinguishes real/fake via a
# server-rendered "doesn't exist" message, and that message disappears
# under X's own bot-mitigation (confirmed during testing — it degrades to
# a generic 200 JS shell for every profile once rate-limited), which a
# shared cloud host IP is likely to trigger quickly. Unlike GitHub/GitLab's
# plain 404, this one would fail silently into false positives in
# production, not just get slower.
PROFILE_URL_TEMPLATES = {
    "GitHub": "https://github.com/{u}",
    "Facebook": "https://www.facebook.com/{u}",
    "GitLab": "https://gitlab.com/{u}",
    "npm": "https://www.npmjs.com/~{u}",
    "Keybase": "https://keybase.io/{u}",
    "SoundCloud": "https://soundcloud.com/{u}",
    "Vimeo": "https://vimeo.com/{u}",
    "Dribbble": "https://dribbble.com/{u}",
    "Behance": "https://www.behance.net/{u}",
    "Flickr": "https://www.flickr.com/people/{u}",
    "DeviantArt": "https://www.deviantart.com/{u}",
    "Letterboxd": "https://letterboxd.com/{u}",
    "Steam": "https://steamcommunity.com/id/{u}",
    "Medium": "https://medium.com/@{u}",
    "Twitch": "https://www.twitch.tv/{u}",
}

NOT_FOUND_MARKERS = {
    "Steam": ["could not be found"],
    "Medium": ["PAGE NOT FOUND"],
    # A missing/fake Twitch channel renders a generic <title>Twitch</title>;
    # a real one always includes the channel name before " - Twitch".
    "Twitch": ["<title>Twitch</title>"],
}

USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_.\-]{1,40}$")

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

    # The exact page the user gave us (e.g. a Linktree/Throne-style profile
    # page) goes first — CANDIDATE_PATHS only adds the site's homepage and
    # common about/contact pages on top of that.
    candidate_urls = list(dict.fromkeys([start_url] + [urljoin(origin, path) for path in CANDIDATE_PATHS]))
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


def looks_like_username(raw: str) -> bool:
    return bool(USERNAME_RE.match(raw)) and "." not in raw


def check_profile_exists(platform: str, url: str):
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return "unknown"
    if resp.status_code == 404:
        return "absent"
    if resp.status_code != 200:
        return "unknown"
    markers = NOT_FOUND_MARKERS.get(platform, [])
    if any(marker in resp.text for marker in markers):
        return "absent"
    return "present"


def probe_username(username: str) -> dict:
    """Checks a bare username against common profile-URL patterns.
    Returns {platform: url} for platforms where a matching profile was
    found. This does NOT confirm the profile belongs to the same person —
    it's a same-username guess across platforms, same as manually checking
    each site by hand."""
    username = username.lstrip("@")
    candidates = {platform: template.format(u=username) for platform, template in PROFILE_URL_TEMPLATES.items()}

    found = {}
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        statuses = pool.map(lambda kv: (kv[0], check_profile_exists(kv[0], kv[1])), candidates.items())
    for platform, status in statuses:
        if status == "present":
            found[platform] = candidates[platform]
    return found


def best_guess_username(results: dict, target_url: str):
    """Picks one likely username to cross-check against other platforms:
    the handle that shows up most often among confirmed links, or failing
    that, the last path segment of the URL the user gave us."""
    handles = Counter()
    for urls in results.values():
        for u in urls:
            segments = [p for p in urlparse(u).path.split("/") if p]
            if segments:
                handles[segments[-1].lstrip("@").lower()] += 1
    if handles:
        return handles.most_common(1)[0][0]

    path_segments = [p for p in urlparse(target_url).path.split("/") if p]
    return path_segments[-1] if path_segments else None


@app.route("/", methods=["GET", "POST"])
def index():
    results = None
    other_matches = None
    pages_checked = None
    error = None
    submitted_url = ""

    if request.method == "POST":
        submitted_url = request.form.get("url", "")
        raw = submitted_url.strip()

        if not raw:
            error = "Enter a URL or username first."

        elif looks_like_username(raw):
            username = raw.lstrip("@")
            other_matches = probe_username(username)
            if not other_matches:
                error = f"No profiles found for username \"{username}\" on the platforms we check."

        else:
            target = normalize_url(raw)
            if not fetch(target):
                error = f"Couldn't reach {target}. Check the URL and try again (some sites block automated requests)."
            else:
                data = find_socials(target)
                results = data["results"]
                pages_checked = data["pages_checked"]

                guess = best_guess_username(results, target)
                if guess and USERNAME_RE.match(guess):
                    already_linked = {u for urls in results.values() for u in urls}
                    probed = probe_username(guess)
                    other_matches = {
                        platform: url for platform, url in probed.items() if url not in already_linked
                    }

                if not results and not other_matches:
                    error = "No social links found on the site, and no matching username found elsewhere."

    return render_template(
        "index.html",
        results=results,
        other_matches=other_matches,
        pages_checked=pages_checked,
        error=error,
        submitted_url=submitted_url,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
