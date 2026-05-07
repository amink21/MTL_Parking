"""
Montreal Token Extractor
=========================
Opens Chrome on the Montreal payment page with the token interceptor
already injected. You type a ticket number and click Suivant — the real
human interaction satisfies reCAPTCHA and the script captures the JWT
automatically from the authorize response.

Usage:
  python get_token.py
  import get_token; token = get_token.fetch_token()
"""

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import time, os, subprocess, socket, tempfile, shutil

SITE_URL   = "https://services.montreal.ca/constats/paiement/recherche-constat"
DB_PATH    = os.path.join(os.path.dirname(__file__), "tickets.db")
DEBUG_PORT = 9222

_INTERCEPT_JS = """
window._capturedToken = null;
(function() {
    var _origOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        if ((url||'').toLowerCase().includes('authorize')) {
            this.addEventListener('load', function() {
                try {
                    var d = JSON.parse(this.responseText);
                    var raw=d.token||d.access_token||d.id_token||d.jwt||'';
                    if (!raw) for(var k in d) if(typeof d[k]==='string'&&d[k].length>40){raw=d[k];break;}
                    if (raw) window._capturedToken=raw.replace('Bearer ','').trim();
                } catch(e){}
            });
        }
        return _origOpen.apply(this, arguments);
    };
    var _origFetch = window.fetch;
    window.fetch = function(url, opts) {
        var p = _origFetch.apply(this, arguments);
        var us = typeof url==='string'?url:(url&&url.url)||'';
        if (us.toLowerCase().includes('authorize')) {
            p.then(function(r){return r.clone().json();}).then(function(d){
                var raw=d.token||d.access_token||d.id_token||d.jwt||'';
                if (!raw) for(var k in d) if(typeof d[k]==='string'&&d[k].length>40){raw=d[k];break;}
                if (raw) window._capturedToken=raw.replace('Bearer ','').trim();
            }).catch(function(){});
        }
        return p;
    };
})();
"""


def _find_chrome():
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    return next((p for p in paths if os.path.exists(p)), None)


def _wait_for_port(host, port, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.5)
    return False


def _poll_token(driver, seconds=180):
    """Wait up to `seconds` for the user to submit the form and capture the token."""
    for i in range(seconds * 2):
        time.sleep(0.5)
        try:
            captured = driver.execute_script("return window._capturedToken;")
            if captured:
                return captured
        except Exception:
            return None
        # Print countdown every 15 seconds
        elapsed = (i + 1) * 0.5
        if elapsed % 15 == 0:
            remaining = seconds - int(elapsed)
            print(f"  Waiting... {remaining}s remaining. (Type a ticket number and click Suivant)")
    return None


def fetch_token(headless=False):  # headless ignored — Chrome always opens visibly
    chrome_path = _find_chrome()
    if not chrome_path:
        print("  ERROR: Chrome not found.")
        return None

    # Kill Chrome so the port is free
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
    time.sleep(2)

    tmp_profile = tempfile.mkdtemp(prefix="mtl_chrome_")

    print(f"  Launching Chrome on port {DEBUG_PORT}...")
    chrome_proc = subprocess.Popen([
        chrome_path,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={tmp_profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--window-size=1100,800",
        SITE_URL,   # open directly on Montreal's page
    ])

    if not _wait_for_port("127.0.0.1", DEBUG_PORT, timeout=15):
        print("  ERROR: Chrome debug port never opened.")
        chrome_proc.terminate()
        return None

    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{DEBUG_PORT}")
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )

    # Inject interceptor for all page loads in this session
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": _INTERCEPT_JS}
    )
    # Also inject into the current page (already loaded)
    driver.execute_script(_INTERCEPT_JS)

    print()
    print("  =" * 30)
    print("  Chrome is open on the Montreal page.")
    print()
    print("  --> Type any ticket number and click Suivant.")
    print("  --> This script will capture the token automatically.")
    print("  =" * 30)
    print()

    token = _poll_token(driver, seconds=180)

    try:
        driver.close()
    except Exception:
        pass
    chrome_proc.terminate()
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
    shutil.rmtree(tmp_profile, ignore_errors=True)
    print("  Chrome closed.")

    return token


def fetch_token_visible():
    return fetch_token()


if __name__ == "__main__":
    print("Montreal JWT Token Extractor")
    print("=" * 50)

    token = fetch_token()

    if token:
        print(f"\n  SUCCESS — token captured!")
        print(f"  Token: {token[:80]}...")
        print(f"\n  Now run the scanner:")
        print(f"  python scanner_v2.py")
    else:
        print(f"\n  No token captured (timed out).")
