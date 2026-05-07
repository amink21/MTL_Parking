"""Debug script — shows all network calls the Montreal page makes."""
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import json, time

SITE_URL = "https://services.montreal.ca/constats/paiement/recherche-constat"

options = Options()
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_experimental_option("excludeSwitches", ["enable-automation"])
options.add_experimental_option("useAutomationExtension", False)
options.add_argument("--window-size=1280,800")
options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
driver.execute_cdp_cmd("Network.enable", {})

print(f"Loading {SITE_URL}...")
driver.get(SITE_URL)
print("Waiting 15 seconds for page + reCAPTCHA...")
time.sleep(15)

print("\n=== ALL API/XHR CALLS ===")
logs = driver.get_log("performance")
seen = set()
for log in logs:
    try:
        msg = json.loads(log["message"])["message"]
        if msg.get("method") == "Network.requestWillBeSent":
            url = msg.get("params", {}).get("request", {}).get("url", "")
            rtype = msg.get("params", {}).get("type", "")
            if rtype in ("XHR", "Fetch") and url not in seen:
                seen.add(url)
                print(f"  [{rtype}] {url}")
    except:
        continue

print("\n=== LOOKING FOR AUTHORIZE/TOKEN RESPONSES ===")
for log in logs:
    try:
        msg = json.loads(log["message"])["message"]
        if msg.get("method") == "Network.responseReceived":
            url = msg.get("params", {}).get("response", {}).get("url", "")
            if any(k in url for k in ("authorize", "token", "auth", "jwt")):
                status = msg.get("params", {}).get("response", {}).get("status", "?")
                req_id = msg["params"]["requestId"]
                print(f"  {status} {url}")
                try:
                    body = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": req_id})
                    print(f"    body: {body.get('body','')[:200]}")
                except Exception as e:
                    print(f"    (body unavailable: {e})")
    except:
        continue

driver.quit()
print("\nDone.")
