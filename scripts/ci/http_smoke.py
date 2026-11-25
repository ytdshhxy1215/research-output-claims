"""Exercise the fixture DOI's first claim and appeal through real HTTP forms."""

import argparse
import json
import os
import re
import time
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

CLAIMS = "/claims/"
LOGIN = "/accounts/login/"
DOI = "10.5555/p1"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


class Page(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.links = set()
        self.csrf = None
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.add(attrs["href"])
        if tag == "input" and attrs.get("name") == "csrfmiddlewaretoken":
            self.csrf = attrs.get("value")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Browser:
    def __init__(self, base_url):
        self.base_url = base_url
        # Disable host proxy settings and check redirects explicitly before following any URL.
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()), NoRedirect())

    def request(self, path, *, fields=None, expected=200):
        parts = urlsplit(path)
        require(parts.path.startswith("/") and not path.startswith("//") and not parts.netloc
                and not parts.scheme and "\\" not in path, f"expected a local path, got {path!r}")
        data = urlencode(fields).encode() if fields is not None else None
        request = Request(self.base_url + path, data, headers={"Referer": self.base_url + path})
        try:
            response = self.opener.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            method = "POST" if data is not None else "GET"
            require(response.status == expected,
                    f"{method} {path}: expected HTTP {expected}, got {response.status}")
            return response.read().decode(response.headers.get_content_charset() or "utf-8"), response.headers

    def get(self, path):
        return self.request(path)[0]

    def form(self, path, fields):
        token = Page(self.get(path)).csrf
        require(token, f"no CSRF form token at {path}")
        _, headers = self.request(path, fields={**fields, "csrfmiddlewaretoken": token}, expected=302)
        return headers.get("Location", "")

    def login(self, username, password):
        _, headers = self.request(CLAIMS, expected=302)
        redirect = headers.get("Location", "")
        parts = urlsplit(redirect)
        require(parts.path == LOGIN and parse_qs(parts.query) == {"next": [CLAIMS]},
                "anonymous claims request did not redirect to login with next=/claims/")
        require(self.form(redirect, {"username": username, "password": password, "next": CLAIMS}) == CLAIMS,
                f"login for {username} did not redirect to {CLAIMS}")
        return self.get(CLAIMS)


def wait_for_slot(browser):
    search = f"{CLAIMS}?{urlencode({'identifier': DOI})}"
    deadline = time.monotonic() + 90
    while True:
        result = browser.get(search)
        slots = []
        for item in re.findall(r"<li\b[^>]*>(.*?)</li>", result, re.DOTALL):
            label = " ".join(unescape(re.sub(r"<[^>]+>", "", item)).split())
            if label.startswith("第 1 位 · 林岚"):
                slots.extend(link for link in Page(item).links if re.fullmatch(r"/claims/slots/\d+/submit/", link))
        if len(slots) == 1 and DOI in result:
            print(f"Worker-ingested fixture {DOI} visible in the claims search.", flush=True)
            return slots[0]
        require(time.monotonic() < deadline, f"no unique first author slot for {DOI} after 90 seconds")
        time.sleep(2)


def expect_detail(browser, case, status):
    body = browser.get(case)
    require(DOI in body and re.search(rf"<dt>\s*当前状态\s*</dt>\s*<dd>\s*{status}\s*</dd>", body),
            f"{case}: expected {DOI} and current state {status}")
    return body


def transition(browser, case, action, fields, state):
    path = f"{case}{action}/"
    require(path in Page(browser.get(case)).links, f"{path} is not offered on the case page")
    require(browser.form(path, fields) == case, f"{path} did not redirect back to the case")
    return expect_detail(browser, case, state)


def expect_report(school):
    body = school.get("/reports/?year=2025&policy=strict")
    totals = re.findall(r"<span\s+data-report-total\s*>\s*(\d+)\s*</span>", body)
    alpha = re.findall(r'<span\s+data-department="alpha"\s*>\s*(\d+)\s*</span>', body)
    beta = re.findall(r'<span\s+data-department="beta"\s*>\s*(\d+)\s*</span>', body)
    require(totals == ["1"] and alpha == ["1"] and beta in ([], ["0"]),
            f"strict 2025 report: expected total=1, alpha=1, beta=0 or omitted; "
            f"got total={totals}, alpha={alpha}, beta={beta}")


def exercise(base_url, password):
    base_url = base_url.rstrip("/")
    origin = urlsplit(base_url)
    require(origin.scheme == "http" and origin.hostname == "127.0.0.1" and origin.port
            and origin.netloc == f"127.0.0.1:{origin.port}" and not (origin.path or origin.query or origin.fragment),
            "expected an HTTP origin at 127.0.0.1:<port>")
    require(len(password) >= 12, "DEMO_PASSWORD must be at least 12 characters")
    teacher = Browser(base_url)
    for endpoint, status in (("/healthz", "ok"), ("/readyz", "ready")):
        require(json.loads(teacher.get(endpoint)).get("status") == status, f"{endpoint} is not {status}")
    teacher.get("/static/admin/css/base.css")
    teacher.login("lin", password)
    slot = wait_for_slot(teacher)
    evidence = {"evidence": "CI synthetic first-author evidence"}
    # A valid login cookie alone must not allow a mutation without the form token.
    teacher.request(slot, fields=evidence, expected=403)
    case = teacher.form(slot, evidence)
    require(re.fullmatch(r"/claims/\d+/", case), "submission did not redirect to a valid claim detail")
    body = expect_detail(teacher, case, "待初审")
    require(re.search(r"<dt>\s*审核管辖\s*</dt>\s*<dd>\s*乙院\s*</dd>", body),
            "lin's first fixture claim was not routed to the beta department")

    alpha = Browser(base_url)
    require(case not in Page(alpha.login("review-alpha", password)).links, "case leaked to alpha queue")
    alpha.request(case, expected=404)
    beta = Browser(base_url)
    require(case in Page(beta.login("review-beta", password)).links, "case missing from beta queue")
    transition(beta, case, "review", {"decision": "reject", "reason": "CI synthetic rejection"}, "初审驳回")
    transition(teacher, case, "appeal", {"reason": "CI synthetic appeal"}, "申诉待复核")
    beta.request(f"{case}resolve/", expected=403)

    school = Browser(base_url)
    require(case in Page(school.login("school-review", password)).links, "case missing from school queue")
    audit = transition(school, case, "resolve", {"decision": "approve", "reason": "CI synthetic resolution"}, "已核准")
    require(all(text in audit for text in ("review-beta", "复核改判核准", "school-review")),
            "audit does not show the expected departmental review and independent resolution")
    expect_detail(teacher, case, "已核准")
    expect_report(school)
    print("HTTP smoke passed: health, readiness, static assets, redirect, CSRF, claim, review, appeal, resolution, and strict report.",
          flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="http://127.0.0.1:<port> of a fresh seeded demo")
    args = parser.parse_args()
    try:
        exercise(args.base_url, os.environ.get("DEMO_PASSWORD", ""))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"HTTP smoke failed: {exc}\n")


if __name__ == "__main__":
    main()
