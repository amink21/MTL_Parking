"""
Montreal Token Extractor
=========================
Uses Selenium to open the real page, let reCAPTCHA v3 execute
naturally, intercept the authorize API call, and extract the JWT token.

Usage:
  python get_token.py          # prints the token
  import get_token; token = get_token.fetch_token()  # use in scanner
"""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from webdriver_manager.chrome import ChromeDriverManager
import json
import time
import re

SITE_URL   = "https://services.montreal.ca/constats/paiement/recherche-constat"
AUTH_URL   = "https://api.montreal.ca/api/justice/ticket/payment/v1/authorize"


def fetch_token(headless=False, timeout=30):
    """
    Opens the Montreal ticket page in Chrome,
    waits for reCAPTCHA to fire, intercepts the authorize call,
    and returns the JWT token.
    
    Returns token string or None.
    """
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
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36")

    # Enable performance logging to capture network requests
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options
    )

    # Mask webdriver detection
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

    token = None

    try:
        print(f"  Loading {SITE_URL}...")
        driver.get(SITE_URL)

        # Wait for page to fully load and reCAPTCHA to execute
        print("  Waiting for reCAPTCHA to execute...")
        time.sleep(5)

        # Check network logs for the authorize call
        print("  Checking network logs...")
        logs = driver.get_log("performance")

        for log in logs:
            try:
                msg = json.loads(log["message"])["message"]

                # Look for the authorize response
                if msg.get("method") == "Network.responseReceived":
                    url = msg.get("params", {}).get("response", {}).get("url", "")
                    if "authorize" in url:
                        req_id = msg["params"]["requestId"]
                        print(f"  Found authorize call: {url}")

                        # Get the response body
                        try:
                            result = driver.execute_cdp_cmd(
                                "Network.getResponseBody",
                                {"requestId": req_id}
                            )
                            body = json.loads(result.get("body", "{}"))
                            raw_token = body.get("token", "")
                            if raw_token:
                                # Strip "Bearer " prefix if present
                                token = raw_token.replace("Bearer ", "").strip()
                                print(f"  Token extracted: {token[:40]}...")
                                break
                        except Exception as e:
                            print(f"  Could not get response body: {e}")

            except Exception:
                continue

        # If not found in logs, try JavaScript extraction
        if not token:
            print("  Trying JS extraction...")
            try:
                # Try to get reCAPTCHA token directly
                recaptcha_token = driver.execute_script("""
                    return new Promise((resolve) => {
                        grecaptcha.ready(function() {
                            grecaptcha.execute('6Le0wk4rAAAAAFVjIuYc45Gp2gyUKg9sVTysjWMb', 
                                {action: 'request_call_back'})
                            .then(function(token) {
                                resolve(token);
                            });
                        });
                    });
                """)
                if recaptcha_token:
                    print(f"  Got reCAPTCHA token: {recaptcha_token[:40]}...")
                    
                    # Now call authorize with this token
                    import requests
                    r = requests.post(AUTH_URL, 
                        json={"key": recaptcha_token},
                        headers={
                            "Origin":       "https://services.montreal.ca",
                            "Referer":      SITE_URL,
                            "Content-Type": "application/json",
                            "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        },
                        timeout=10
                    )
                    if r.status_code in (200, 201):
                        raw = r.json().get("token", "")
                        token = raw.replace("Bearer ", "").strip()
                        print(f"  JWT token obtained: {token[:40]}...")
            except Exception as e:
                print(f"  JS extraction error: {e}")

        # Last resort: try intercepting via CDP
        if not token:
            print("  Trying CDP interception...")
            try:
                driver.execute_cdp_cmd("Network.enable", {})
                
                # Trigger a page reload to capture fresh authorize call
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
                                    "Network.getResponseBody",
                                    {"requestId": req_id}
                                )
                                body = json.loads(result.get("body", "{}"))
                                raw_token = body.get("token", "")
                                if raw_token:
                                    token = raw_token.replace("Bearer ", "").strip()
                                    print(f"  Token via CDP: {token[:40]}...")
                                    break
                    except:
                        continue
            except Exception as e:
                print(f"  CDP error: {e}")

    finally:
        driver.quit()
        print("  Chrome closed.")

    return token


def fetch_token_visible():
    """Same but with visible browser window for debugging"""
    return fetch_token(headless=False)


if __name__ == "__main__":
    import sys
    headless = "--visible" not in sys.argv

    print("Montreal JWT Token Extractor")
    print("="*50)
    if not headless:
        print("Mode: VISIBLE browser")
    else:
        print("Mode: headless (use --visible to see browser)")
    print()

    token = fetch_token(headless=headless)

    if token:
        print(f"\n✅ SUCCESS!")
        print(f"Token: {token[:80]}...")
        print(f"\nYou can now run the scanner:")
        print(f"  python scanner_v2.py")
    else:
        print(f"\n❌ Could not extract token.")
        print(f"Try running with visible browser:")
        print(f"  python get_token.py --visible")