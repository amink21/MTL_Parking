"""
Montreal Token Extractor
=========================
Injects a fetch interceptor before page load so the JWT from the
authorize call is captured directly in the page's JS context.
Works in both headless (CI) and visible (local) mode.

Usage:
  python get_token.py               # headless
  python get_token.py --visible     # visible browser
  import get_token; token = get_token.fetch_token()
"""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import time

SITE_URL = "https://services.montreal.ca/constats/paiement/recherche-constat"

# Injected before page scripts run — intercepts fetch + XHR and stores the JWT
_INTERCEPTOR = """
window.__jwt_token = null;

const _origFetch = window.fetch;
window.fetch = async function(...args) {
    const resp = await _origFetch.apply(this, args);
    try {
        const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
        if (url.includes('authorize')) {
            resp.clone().json().then(function(d) {
                const t = d && (d.token || d.access_token || d.jwt);
                if (t) window.__jwt_token = t.replace(/^Bearer /, '').trim();
            }).catch(function(){});
        }
    } catch(e) {}
    return resp;
};

const _OrigXHR = window.XMLHttpRequest;
window.XMLHttpRequest = function() {
    const xhr = new _OrigXHR();
    xhr.addEventListener('load', function() {
        try {
            if (xhr.responseURL && xhr.responseURL.includes('authorize')) {
                const d = JSON.parse(xhr.responseText);
                const t = d && (d.token || d.access_token);
                if (t) window.__jwt_token = t.replace(/^Bearer /, '').trim();
            }
        } catch(e) {}
    });
    return xhr;
};
"""


def fetch_token(headless=False, timeout=40):
    print("  Starting Chrome...")

    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1280,800")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    token = None
    try:
        # Inject interceptor so it runs before any page script
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": _INTERCEPTOR},
        )

        print(f"  Loading {SITE_URL}...")
        driver.get(SITE_URL)

        # Poll for the token — reCAPTCHA fires within ~10 s normally
        print("  Waiting for reCAPTCHA + authorize call...")
        for i in range(timeout):
            token = driver.execute_script("return window.__jwt_token;")
            if token:
                print(f"  Token captured at {i+1}s: {token[:40]}...")
                break
            time.sleep(1)

        if not token:
            print("  Token not captured — page may have changed or reCAPTCHA blocked.")

    finally:
        driver.quit()
        print("  Chrome closed.")

    return token


def fetch_token_visible():
    return fetch_token(headless=False)


if __name__ == "__main__":
    import sys
    headless = "--visible" not in sys.argv

    print("Montreal JWT Token Extractor")
    print("=" * 50)
    print(f"Mode: {'headless' if headless else 'VISIBLE'}\n")

    token = fetch_token(headless=headless)

    if token:
        print(f"\n✅ SUCCESS")
        print(f"Token: {token[:80]}...")
        print(f"\nRun the scanner:")
        print(f"  python scanner_v2.py")
    else:
        print(f"\n❌ Could not extract token.")
        print(f"Try: python get_token.py --visible")
