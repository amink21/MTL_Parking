"""
Montreal Token Extractor
=========================
Submits a ticket number on the payment search page to trigger the
authorize call, then captures the JWT via a JS fetch interceptor.

Usage:
  python get_token.py          # headless
  python get_token.py --visible
  import get_token; token = get_token.fetch_token()
"""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import time, sqlite3, os

SITE_URL = "https://services.montreal.ca/constats/paiement/recherche-constat"
DB_PATH  = os.path.join(os.path.dirname(__file__), "tickets.db")

# Injected before ANY page script — overrides fetch to capture the authorize response body
_INTERCEPT_JS = """
window._capturedToken = null;
window._interceptLog  = [];

(function() {
    var _origFetch = window.fetch;
    window.fetch = function(url, opts) {
        var p = _origFetch.apply(this, arguments);
        var urlStr = (typeof url === 'string') ? url : (url && url.url) || '';
        window._interceptLog.push('[fetch] ' + urlStr);
        if (urlStr.toLowerCase().includes('authorize')) {
            p.then(function(r) {
                var clone = r.clone();
                return clone.json();
            }).then(function(d) {
                var raw = d.token || d.access_token || d.id_token || d.jwt || '';
                if (!raw) {
                    // look one level deeper in case token is nested
                    var keys = Object.keys(d);
                    for (var i = 0; i < keys.length; i++) {
                        var v = d[keys[i]];
                        if (typeof v === 'string' && v.length > 40) { raw = v; break; }
                    }
                }
                if (raw) {
                    window._capturedToken = raw.replace('Bearer ', '').trim();
                    window._interceptLog.push('[fetch] CAPTURED TOKEN from authorize');
                } else {
                    window._interceptLog.push('[fetch] authorize 200 but no token field. Keys: ' + JSON.stringify(Object.keys(d)));
                }
            }).catch(function(e) {
                window._interceptLog.push('[fetch] authorize clone/parse error: ' + e);
            });
        }
        return p;
    };

    var _origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        var urlStr = url || '';
        window._interceptLog.push('[xhr] ' + urlStr);
        if (urlStr.toLowerCase().includes('authorize')) {
            this.addEventListener('load', function() {
                try {
                    var d = JSON.parse(this.responseText);
                    var raw = d.token || d.access_token || d.id_token || d.jwt || '';
                    if (raw) {
                        window._capturedToken = raw.replace('Bearer ', '').trim();
                        window._interceptLog.push('[xhr] CAPTURED TOKEN from authorize');
                    } else {
                        window._interceptLog.push('[xhr] authorize 200 but no token. Keys: ' + JSON.stringify(Object.keys(d)));
                    }
                } catch(e) {
                    window._interceptLog.push('[xhr] parse error: ' + e);
                }
            });
        }
        return _origOpen.apply(this, arguments);
    };
})();
"""


def _get_test_tickets(n=5):
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT ticket_number FROM tickets ORDER BY scraped_at DESC LIMIT ?", (n,)
        ).fetchall()
        conn.close()
        nums = [r[0] for r in rows]
        return nums if nums else ["918431345", "918431334", "918431320"]
    except Exception:
        return ["918431345", "918431334", "918431320"]


def _poll_token(driver, seconds=18):
    for _ in range(seconds * 2):
        time.sleep(0.5)
        captured = driver.execute_script("return window._capturedToken;")
        if captured:
            return captured
    return None


def _dump_intercept_log(driver):
    try:
        log = driver.execute_script("return window._interceptLog || [];")
        if log:
            print("  Intercept log:")
            for line in log[-20:]:
                print(f"    {line}")
    except Exception:
        pass


def _try_ticket(driver, ticket_num):
    """Navigate to page, fill in ticket, click Suivant, wait for token."""
    print(f"\n  --- Trying ticket #{ticket_num} ---")
    driver.get(SITE_URL)
    time.sleep(1)

    # Dismiss cookie consent
    try:
        accept_btn = WebDriverWait(driver, 6).until(
            EC.element_to_be_clickable((By.XPATH,
                "//*[contains(@class,'accept') or contains(@id,'accept') or "
                "contains(text(),'Accept') or contains(text(),'Accepter') or "
                "contains(text(),'accepter')]"
            ))
        )
        accept_btn.click()
        print("  Cookie banner dismissed.")
        time.sleep(1)
    except Exception:
        pass

    # Find the ticket input
    inp = None
    for selector in [
        "input[type='text']",
        "input[name*='constat']", "input[id*='constat']",
        "input[name*='numero']", "input[id*='numero']",
        "input[placeholder*='constat']", "input[placeholder*='numéro']",
        "input",
    ]:
        try:
            c = WebDriverWait(driver, 4).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, selector))
            )
            if c.is_displayed():
                inp = c
                break
        except Exception:
            continue

    if not inp:
        print("  No input field found.")
        return None

    inp.clear()
    inp.send_keys(ticket_num)
    print("  Typed ticket number.")
    time.sleep(0.5)

    # Click Suivant
    clicked = False
    for xpath in [
        "//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'suivant')]",
        "//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'next')]",
        "//button[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'recherch')]",
        "//button[@type='submit']",
        "//input[@type='submit']",
    ]:
        try:
            btn = WebDriverWait(driver, 4).until(
                EC.element_to_be_clickable((By.XPATH, xpath))
            )
            label = btn.text.strip() or xpath
            print(f"  Clicking: '{label}'")
            btn.click()
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        try:
            for b in driver.find_elements(By.TAG_NAME, "button"):
                if b.is_displayed():
                    print(f"  Fallback click: '{b.text}'")
                    b.click()
                    clicked = True
                    break
        except Exception:
            pass

    if not clicked:
        print("  No clickable button found.")
        return None

    print("  Waiting for authorize call (18s)...")
    token = _poll_token(driver, seconds=18)

    if not token:
        _dump_intercept_log(driver)

    return token


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

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    # Inject interceptor before ANY page script on every navigation
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": _INTERCEPT_JS}
    )

    token = None
    tickets = _get_test_tickets(5)
    print(f"  Will try tickets: {tickets[:3]}")

    try:
        for ticket_num in tickets[:3]:
            token = _try_ticket(driver, ticket_num)
            if token:
                print(f"  Token captured: {token[:60]}...")
                break
            print(f"  No token for #{ticket_num}, trying next...")
            time.sleep(2)
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
        print(f"\n SUCCESS")
        print(f"Token: {token[:80]}...")
        print(f"\nRun the scanner:")
        print(f"  python scanner_v2.py")
    else:
        print(f"\n Could not extract token.")
        print(f"Try: python get_token.py --visible")
