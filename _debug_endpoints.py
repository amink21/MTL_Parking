"""
Capture ALL API calls made by the Montreal payment portal after you look up a ticket.

Run this, type a real ticket number and click Suivant, then let it sit for 10 seconds.
It will print every XHR/Fetch URL and its full JSON response body.

Usage:
  python _debug_endpoints.py
"""

import os, subprocess, socket, tempfile, shutil, json, time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

SITE_URL   = "https://services.montreal.ca/constats/paiement/recherche-constat"
DEBUG_PORT = 9223   # different port so it doesn't conflict with get_token.py

_CAPTURE_JS = """
window._apiCalls = [];
(function() {
    var _origOpen = XMLHttpRequest.prototype.open;
    var _origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function(method, url) {
        this._captureUrl = url;
        this._captureMethod = method;
        return _origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
        var self = this;
        this.addEventListener('load', function() {
            try {
                window._apiCalls.push({
                    url:    self._captureUrl,
                    method: self._captureMethod,
                    status: self.status,
                    body:   self.responseText.substring(0, 4000),
                    ts:     Date.now(),
                });
            } catch(e) {}
        });
        return _origSend.apply(this, arguments);
    };

    var _origFetch = window.fetch;
    window.fetch = function(url, opts) {
        var us = typeof url === 'string' ? url : (url && url.url) || '';
        var method = (opts && opts.method) || 'GET';
        var p = _origFetch.apply(this, arguments);
        p.then(function(r) {
            return r.clone().text();
        }).then(function(text) {
            window._apiCalls.push({
                url:    us,
                method: method,
                status: 0,
                body:   text.substring(0, 4000),
                ts:     Date.now(),
            });
        }).catch(function() {});
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


def run():
    chrome_path = _find_chrome()
    if not chrome_path:
        print("ERROR: Chrome not found.")
        return

    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
    time.sleep(2)

    tmp_profile = tempfile.mkdtemp(prefix="mtl_debug_")
    print(f"Launching Chrome on port {DEBUG_PORT}...")
    chrome_proc = subprocess.Popen([
        chrome_path,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={tmp_profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--window-size=1200,900",
        SITE_URL,
    ])

    if not _wait_for_port("127.0.0.1", DEBUG_PORT, timeout=15):
        print("ERROR: Chrome debug port never opened.")
        chrome_proc.terminate()
        return

    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{DEBUG_PORT}")
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=options,
    )

    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _CAPTURE_JS})
    driver.execute_script(_CAPTURE_JS)

    print()
    print("=" * 60)
    print("Chrome is open on the Montreal payment page.")
    print()
    print("  1. Type a REAL ticket number")
    print("  2. Click Suivant (pass reCAPTCHA naturally)")
    print("  3. Wait for the ticket details to appear on screen")
    print("  4. Then come back here and press ENTER")
    print("=" * 60)
    input("\nPress ENTER after the ticket details are visible on screen: ")

    # Extra 3 seconds for any deferred fetches
    time.sleep(3)

    calls = driver.execute_script("return window._apiCalls || [];")

    try:
        driver.close()
    except Exception:
        pass
    chrome_proc.terminate()
    subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
    shutil.rmtree(tmp_profile, ignore_errors=True)

    print(f"\nCaptured {len(calls)} API call(s):\n")
    for i, call in enumerate(calls):
        url    = call.get("url", "")
        method = call.get("method", "?")
        status = call.get("status", "?")
        body   = call.get("body", "")

        # Skip noise (analytics, fonts, Google scripts, etc.)
        skip_patterns = ("google", "recaptcha", "gtag", "analytics", "fonts", "cdn", "static", ".js", ".css", ".png", ".svg")
        if any(p in url.lower() for p in skip_patterns):
            continue

        print(f"[{i+1}] {method} {status}  {url}")
        # Try to pretty-print JSON
        try:
            parsed = json.loads(body)
            pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
            # Show full body — truncate only if enormous
            if len(pretty) > 3000:
                print(f"  (body truncated to 3000 chars)")
                print(pretty[:3000])
            else:
                print(pretty)
        except Exception:
            if body:
                print(f"  (raw) {body[:500]}")
        print()

    # Save full log to file
    log_path = os.path.join(os.path.dirname(__file__), "_endpoint_log.json")
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(calls, f, indent=2, ensure_ascii=False)
    print(f"Full log saved to: {log_path}")


if __name__ == "__main__":
    run()
