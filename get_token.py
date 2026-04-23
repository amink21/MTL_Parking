"""
Montreal Token Extractor
=========================
Uses Selenium to open the real page, let reCAPTCHA v3 execute
naturally, intercept the authorize API call, and extract the JWT token.

Usage:
  python get_token.py          # headless
  python get_token.py --visible
  import get_token; token = get_token.fetch_token()
"""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import json
import time

SITE_URL = "https://services.montreal.ca/constats/paiement/recherche-constat"
AUTH_URL = "https://api.montreal.ca/api/justice/ticket/payment/v1/authorize"


def fetch_token(headless=False):
    print("  Starting Chrome...")

    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--window-size=1280,800")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    token = None

    try:
        print(f"  Loading {SITE_URL}...")
        driver.get(SITE_URL)

        print("  Waiting for reCAPTCHA to execute...")
        time.sleep(5)

        # ── Method 1: CDP performance logs ──────────────────────────
        print("  Checking network logs...")
        logs = driver.get_log("performance")
        for log in logs:
            try:
                msg = json.loads(log["message"])["message"]
                if msg.get("method") == "Network.responseReceived":
                    url = msg.get("params", {}).get("response", {}).get("url", "")
                    if "authorize" in url:
                        req_id = msg["params"]["requestId"]
                        print(f"  Found authorize call: {url}")
                        try:
                            result = driver.execute_cdp_cmd(
                                "Network.getResponseBody", {"requestId": req_id}
                            )
                            body = json.loads(result.get("body", "{}"))
                            raw = body.get("token", "")
                            if raw:
                                token = raw.replace("Bearer ", "").strip()
                                print(f"  Token extracted: {token[:40]}...")
                                break
                        except Exception as e:
                            print(f"  Could not get response body: {e}")
            except Exception:
                continue

        # ── Method 2: JS reCAPTCHA + direct POST ────────────────────
        if not token:
            print("  Trying JS extraction...")
            try:
                import requests as _req
                recaptcha_token = driver.execute_script("""
                    return new Promise((resolve) => {
                        grecaptcha.ready(function() {
                            grecaptcha.execute('6Le0wk4rAAAAAFVjIuYc45Gp2gyUKg9sVTysjWMb',
                                {action: 'request_call_back'})
                            .then(resolve);
                        });
                    });
                """)
                if recaptcha_token:
                    print(f"  Got reCAPTCHA token: {recaptcha_token[:40]}...")
                    r = _req.post(
                        AUTH_URL,
                        json={"key": recaptcha_token},
                        headers={
                            "Origin":       "https://services.montreal.ca",
                            "Referer":      SITE_URL,
                            "Content-Type": "application/json",
                            "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                        },
                        timeout=10,
                    )
                    if r.status_code in (200, 201):
                        raw = r.json().get("token", "")
                        token = raw.replace("Bearer ", "").strip()
                        print(f"  JWT token obtained: {token[:40]}...")
            except Exception as e:
                print(f"  JS extraction error: {e}")

        # ── Method 3: reload + CDP retry ────────────────────────────
        if not token:
            print("  Trying CDP reload interception...")
            try:
                driver.execute_cdp_cmd("Network.enable", {})
                driver.refresh()
                time.sleep(5)
                logs = driver.get_log("performance")
                for log in logs:
                    try:
                        msg = json.loads(log["message"])["message"]
                        if msg.get("method") == "Network.responseReceived":
                            url = msg.get("params", {}).get("response", {}).get("url", "")
                            if "authorize" in url:
                                req_id = msg["params"]["requestId"]
                                result = driver.execute_cdp_cmd(
                                    "Network.getResponseBody", {"requestId": req_id}
                                )
                                body = json.loads(result.get("body", "{}"))
                                raw = body.get("token", "")
                                if raw:
                                    token = raw.replace("Bearer ", "").strip()
                                    print(f"  Token via CDP reload: {token[:40]}...")
                                    break
                    except Exception:
                        continue
            except Exception as e:
                print(f"  CDP reload error: {e}")

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
