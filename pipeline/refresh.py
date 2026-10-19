"""Refresh the KitchenAid demo dashboard data from Salesforce.

Runs in GitHub Actions (see .github/workflows/refresh.yml) or locally:
  SF_DOMAIN=meshcircle.my.salesforce.com SF_CLIENT_ID=... SF_CLIENT_SECRET=... DASHBOARD_PASSWORD=... python pipeline/refresh.py
  python pipeline/refresh.py --fixtures tests/fixtures --today 2026-09-28      (offline test with recorded responses)

Writes data.enc.json (AES-256-GCM, key from the dashboard password). Nothing readable is published.
Internal warnings (drafts, duplicates, redactions, missing prices, HACCP) go to the GitHub job summary, never to the site.
Prices: kitchenaid.com.au is read on every run (RRP = 'was' price if on promo, else price); each sale keeps the RRP of its own date (price history inside the encrypted files); unknown products are matched by name and listed on the checks page.
If Salesforce can't be reached, the previous data file is left untouched and the job fails (GitHub emails the repo owner).
"""
import argparse, base64, csv, datetime as dt, json, os, re, sys
from collections import Counter, defaultdict
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "pipeline" / "config.json").read_text(encoding="utf-8"))
API = "v67.0"
WARN = []
# retailer staff names -> roles: kept in a GitHub secret (REDACT_NAMES, JSON) so no names sit in the public repo
NAMES = json.loads(os.environ.get("REDACT_NAMES") or "{}")
def warn(msg): WARN.append(msg)          # never printed: public repo logs are public

# ---------------- Salesforce ----------------
class Salesforce:
    def __init__(self, domain, client_id, client_secret):
        import requests
        self.rq = requests
        r = requests.post(f"https://{domain}/services/oauth2/token", timeout=30,
                          data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
        if r.status_code != 200:
            raise SystemExit(f"Salesforce login failed ({r.status_code}): {r.text[:300]}")
        tok = r.json(); self.base = tok["instance_url"]; self.h = {"Authorization": f"Bearer {tok['access_token']}"}

    def query(self, key, soql):
        out, url, params = [], f"{self.base}/services/data/{API}/query", {"q": soql}
        while url:
            r = self.rq.get(url, headers=self.h, params=params, timeout=60)
            if r.status_code != 200:
                raise SystemExit(f"Salesforce query '{key}' failed ({r.status_code}): {r.text[:300]}")
            j = r.json(); out += j["records"]
            url = (self.base + j["nextRecordsUrl"]) if not j.get("done", True) else None; params = None
        return out

class Fixtures:
    """Recorded Salesforce responses for offline tests: <dir>/<key>.json, REST query format."""
    def __init__(self, d): self.d = Path(d)
    def query(self, key, soql):
        recs = []
        for f in sorted(self.d.glob(f"{key}*.json")):
            recs += json.loads(f.read_text())["body"]["records"]
        return recs

# ---------------- helpers ----------------
def monday(d): return d - dt.timedelta(days=d.weekday())
def first_num(s):
    m = re.search(r"\d+", s or ""); return int(m.group()) if m else None
def traffic(s):
    s = (s or "").lower(); lv = {"light": 1, "steady": 2, "medium": 2, "busy": 3, "high": 3}
    has = [v for k, v in lv.items() if k in s]
    return {1: "Light", 2: "Medium", 3: "Busy"}[min(has)] if has else "Other"   # conservative: lowest level mentioned
def title_store(s): return " ".join(w if w.upper() in ("JB", "HN", "DJ") else w.capitalize() for w in s.split())
def staff_name(first, last):
    m = re.search(r"\(([^)]+)\)", first or ""); f = m.group(1) if m else (first or "").split()[0] if first else "?"
    return f"{f} {(last or '?')[0]}."
def retailer(store):
    s = store.upper()
    for k, v in (("POP-UP", "KitchenAid Pop-Up"), ("MYER", "Myer"), ("DAVID JONES", "David Jones"), ("GOOD GUYS", "The Good Guys"),
                 ("KITCHEN WAREHOUSE", "Kitchen Warehouse"), ("HARVEY NORMAN", "Harvey Norman"), ("BING LEE", "Bing Lee"), ("JB", "JB Hi-Fi")):
        if k in s: return v
    return "Other"
def model_of(text):
    t = text.upper()
    for m in ("KF2", "KF3", "KF4", "KF6", "KF7", "KF8"):
        if m in t: return m
    return "Semi" if "SEMI AUTOMATIC" in t or "SEMI-AUTOMATIC" in t else None
def category(text):
    t = text.upper(); m = model_of(t)
    if m: return m
    if "GO CORDLESS" in t or "CORDLESS GO" in t: return "Cordless"
    if "HAND MIXER" in t or "HAND BLENDER" in t: return "Handheld"
    if "MIXER" in t: return "Stand Mixers"
    if "TOASTER" in t or "KETTLE" in t: return "Breakfast"
    if "FOOD PROCESSOR" in t or "CHOPPER" in t: return "Food Processors"
    if "BLENDER" in t: return "Blenders"
    return "Attachments & Accessories"
def clean_product_name(p):
    return re.sub(r"^\s*\S*\d\S*\s*-\s*", "", p).replace("®", " ").strip() if re.match(r"^\s*\S*\d\S*\s*-", p) else p.replace("®", " ").strip()

NM = r"[A-Z][A-Za-z'\-]+"
ROLE_WORDS = r"(?i:manager|supervisor|csm|leader|asst|assistant|2ic|staff member)"   # case-insensitive here only
NOT_NAMES = {"Yes", "No", "The", "Store", "Staff", "Myer", "Customer", "Customers", "Floor", "Section", "Team", "Asst", "Store Manager", "Both", "Also", "And"}
FIRST_NAMES = {l.strip() for l in (ROOT / "pipeline" / "first_names.txt").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")}
LEARNED = {}        # names found next to a role this run, e.g. "Zelda (Section Manager)" -> Zelda = the manager

def learn_names(texts):
    """Find people's names from context, so they are removed everywhere without anyone listing them first."""
    for t in texts:
        if not t: continue
        for m in re.finditer(rf"\b({NM})(?:\s+(?:and|&)\s+({NM}))?\s*\(([^)]*?{ROLE_WORDS}[^)]*)\)", t):
            for n in (m.group(1), m.group(2)):
                if n and n[0].isupper() and n not in NOT_NAMES: LEARNED.setdefault(n, "the manager")
        for m in re.finditer(rf"{ROLE_WORDS}s?\s*\(({NM})\)", t):
            if m.group(1) not in NOT_NAMES: LEARNED.setdefault(m.group(1), "the manager")
        for m in re.finditer(rf"\b(?:[Cc]ustomer|[Cc]ust)\s+({NM})\b", t):
            n = m.group(1)   # only real first names: "a cust Zara" yes, "customer Most people" no
            if n in FIRST_NAMES and n not in NOT_NAMES and n not in CFG.get("not_names", []): LEARNED.setdefault(n, "the customer")

def redact_roles(out):
    """'Zelda and Quincy (Section Managers)' -> 'the Section Managers'; 'Quincy (one of the managers)' -> 'one of the managers';
    'one of the managers (Zelda)' -> 'one of the managers'."""
    def name_role(m):
        role = m.group(1).strip()
        return role if role.lower().startswith(("one of", "a ", "an ", "the ")) else "the " + role
    out = re.sub(rf"\b{NM}(?:\s+(?:and|&)\s+{NM})?\s*\(([^)]*?{ROLE_WORDS}[^)]*)\)", name_role, out)
    out = re.sub(rf"({ROLE_WORDS}s?)\s*\({NM}\)", r"\1", out)
    return out

def redact(text, sid):
    if not text: return text
    out = redact_roles(text)
    for name, role in sorted({**LEARNED, **NAMES}.items(), key=lambda kv: -len(kv[0])):
        def put(m, role=role):    # capitalise at the start of a sentence: "Quincy promised" -> "The manager promised"
            start = m.start() == 0 or re.search(r"[.!?]\s*$|\n\s*$", out[:m.start()])
            return role[0].upper() + role[1:] if start else role
        if role == "the customer":   # "a cust Zara" -> "a cust"; "(Zara & Hubby)" -> dropped
            out = re.sub(rf"\b([Cc]ust(?:omer)?)\.?\s+{re.escape(name)}\b", r"\1", out)
            out = re.sub(rf"\s*\([^()]*\b{re.escape(name)}\b[^()]*\)", "", out)
        out = re.sub(rf"\b{re.escape(name)}(?![\w])", put, out)
    # tidy doubled replacements: "a staff member and a staff member" -> "two staff members"
    out = re.sub(r"\b([Aa]|[Tt]he) (staff member|manager) and (?:a|the) \2\b", lambda m: ("T" if m.group(1)[0].isupper() else "t") + "wo " + m.group(2) + "s", out)
    out = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[removed]", out)
    out = re.sub(r"\b(?:\+?61|0)[2-478](?:[ -]?\d){8}\b", "[removed]", out)
    for term in CFG["redact"]["health_terms"]:
        if term in out.lower():
            new = re.sub(rf"\s*\([^)]*{term}[^)]*\)", "", out, flags=re.I)
            if new == out:  # not in brackets: drop the sentence
                new = re.sub(rf"[^.!?\n]*{term}[^.!?\n]*[.!?]?", "[detail removed]", out, flags=re.I)
            out = new; warn(f"Shift {sid}: removed a health-related detail from an answer — check it reads correctly.")
    return out

def review(text, sid, where):
    """Internal flags only (checks page). Nothing here changes what the client sees."""
    if not text: return
    for term in CFG.get("review_terms", []):
        m = re.search(rf"\b{re.escape(term)}\b", text, re.I)
        if m:
            a = max(0, m.start() - 50); warn(f"Review before sharing: shift {where} mentions '{term}': …{text[a:m.end() + 50].strip()}…"); break
    plain = re.sub(r"\bDavid\s+Jones\b", "", text, flags=re.I)        # the store, not a person
    for w in sorted(set(re.findall(r"\b([A-Z][a-z]+)\b", plain))):
        if w in FIRST_NAMES and w not in CFG.get("not_names", []):
            warn(f"Possible person's name '{w}' in shift {where}. If it's a person, add it to the REDACT_NAMES secret.")

def token_health(token, repo, today):
    """Check the Refresh-button token. Returns (usable, public note). Fine-grained tokens report their expiry in a response header."""
    import requests
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}", headers={"Authorization": f"Bearer {token}"}, timeout=20)
    except Exception:
        return True, ""
    if r.status_code in (401, 403):
        warn("The DISPATCH_TOKEN (Refresh button) is invalid or expired: Refresh now only reloads saved data. Create a new token and update the secret.")
        return False, "Refresh-button token invalid or expired."
    exp = r.headers.get("github-authentication-token-expiration")
    if exp:
        days = (dt.date.fromisoformat(exp[:10]) - today).days
        if days <= 21:
            warn(f"The DISPATCH_TOKEN (Refresh button) expires in {days} days ({exp[:10]}). Create a new token and update the secret before then.")
            return True, f"Refresh-button token expires {exp[:10]}."
    return True, ""

# ---------------- prices ----------------
CAT_URL = "https://kitchenaid.com.au/products.json"
def rrp_of(c):
    """RRP = the 'was' price when on promotion, else the selling price."""
    p = float(c["price"] or 0); ca = float(c["compare_at"] or 0)
    return max(p, ca)

def fetch_catalogue():
    """Current AU catalogue from kitchenaid.com.au (public Shopify feed). None if unreachable or implausibly small."""
    try:
        import requests
        rows = []
        for page in range(1, 11):
            r = requests.get(CAT_URL, params={"limit": 250, "page": page}, timeout=30, headers={"User-Agent": "Mesh-KitchenAid-dashboard/1.0"})
            r.raise_for_status(); prods = r.json().get("products", [])
            if not prods: break
            for p in prods:
                for v in p.get("variants", []):
                    rows.append({"handle": p["handle"], "title": p["title"], "type": p.get("product_type", ""),
                                 "variant": "" if v.get("title") == "Default Title" else v.get("title", ""), "sku": (v.get("sku") or "").strip(),
                                 "price": v.get("price") or "0", "compare_at": v.get("compare_at_price") or "", "available": "1" if v.get("available") else "0"})
        return rows if len(rows) >= 100 else None
    except Exception:
        return None

SEED_DATE = "2000-01-01"     # "since before tracking started"
def seed_history(rows, checked):
    """Start the price history from a saved catalogue: those prices apply to every earlier sale."""
    return {"checked": checked, "skus": {r["sku"].upper(): [[SEED_DATE, rrp_of(r)]] for r in rows if r["sku"]}}

def update_history(hist, rows, today):
    """Record today's website prices. A product's RRP change is dated today, so earlier sales keep the price they were sold at.
    Returns a list of (sku, title, old, new) changes."""
    changes, day = [], today.isoformat()
    for r in rows:
        if not r["sku"]: continue
        sku, p = r["sku"].upper(), rrp_of(r)
        h = hist["skus"].setdefault(sku, [])
        if not h: h.append([SEED_DATE, p]); continue          # new on the website: no earlier price to keep
        if abs(h[-1][1] - p) < 0.005: continue
        old = h[-1][1]
        if h[-1][0] == day:                                   # changed again the same day: keep the latest
            h[-1][1] = p
            if len(h) > 1 and abs(h[-2][1] - p) < 0.005: h.pop()
        else: h.append([day, p])
        changes.append((sku, f"{r['title']} {r['variant']}".strip(), old, p))
    hist["checked"] = day
    return changes

def rrp_at(hist, sku, day):
    """RRP in force on a sale date (the earliest known price for sales before tracking started). None if unknown."""
    h = hist["skus"].get((sku or "").upper())
    if not h: return None
    d, best = day.isoformat(), h[0][1]
    for since, p in h:
        if since <= d: best = p
    return best

def unseal(blob, password):
    """Decrypt a published file; None if missing, damaged or the password changed."""
    if not password: return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        d = lambda k: base64.b64decode(blob[k])
        key = PBKDF2HMAC(hashes.SHA256(), 32, d("s"), int(blob["n"])).derive(password.encode())
        return json.loads(AESGCM(key).decrypt(d("i"), d("c"), None))
    except Exception:
        return None

_STOP = {"KITCHENAID", "THE", "WITH", "AND", "OF", "FOR", "IN"}
_SYN = {"MATT": "MATTE", "GREY": "GRAY"}
_MODEL = re.compile(r"K[A-Z]{1,5}\d{2,5}[A-Z0-9]*")
def _toks(s):
    t = re.findall(r"[A-Z0-9][A-Z0-9.]*", s.upper().replace("®", " ").replace("™", " "))
    t = [re.sub(r"^5(K[A-Z]{1,5}\d)", r"\1", _SYN.get(w, w).strip(".")) for w in t]
    return {w for w in t if w and w not in _STOP}

def auto_match(name, rows):
    """Match a Salesforce product name to a website product by words (model, type, colour).
    Tested on the hand-made price map: 42 of 43 right, 0 wrong, 1 left unmatched. Returns (row, loose) or None.
    Unsure → None (shows 'no price found') rather than a wrong price. Several equally good matches → lowest price, marked loose."""
    S = _toks(name)
    if not S: return None
    models = {w for w in S if _MODEL.fullmatch(w)}
    scored = []
    for c in rows:
        C = _toks(c["title"] + " " + c["variant"])
        if ("BUNDLE" in C or "+" in c["title"]) and "BUNDLE" not in S: continue
        if models and not models & C: continue
        inter = len(S & C)
        if inter: scored.append((inter / len(S | C), inter / len(S), c))
    if not scored: return None
    j, cov, _ = max(scored, key=lambda x: x[0])
    if cov < 0.75 or j < 0.5: return None
    ties = [x[2] for x in scored if x[0] >= j - 1e-9]
    best = min(ties, key=rrp_of)
    return best, (len({rrp_of(c) for c in ties}) > 1 or j < 0.7)

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures"); ap.add_argument("--today"); ap.add_argument("--out", default=str(ROOT / "data.enc.json"))
    ap.add_argument("--plain", help="also write the unencrypted JSON here (local testing only, never commit)")
    a = ap.parse_args()
    public_note = []
    tz = ZoneInfo(CFG["timezone"]); now = dt.datetime.now(tz)
    today = dt.date.fromisoformat(a.today) if a.today else now.date()
    start = monday(today) - dt.timedelta(weeks=CFG["weeks_back"] - 1)
    start = min(start, start.replace(day=1))   # begin on the 1st so the Month filter shows whole months
    sf = Fixtures(a.fixtures) if a.fixtures else Salesforce(os.environ["SF_DOMAIN"], os.environ["SF_CLIENT_ID"], os.environ["SF_CLIENT_SECRET"])
    pw = os.environ.get("DASHBOARD_PASSWORD") or ("test-password-only" if a.fixtures else None)
    if not pw or len(pw) < 14: raise SystemExit("DASHBOARD_PASSWORD missing or too short (min 14 characters).")

    accts = ",".join(f"'{x}'" for x in CFG["accounts"])
    F = lambda p="": (f"{p}RB_Customer__r.Name IN ({accts}) AND {p}Business_Unit_Text__c = '{CFG['business_unit']}' "
                      f"AND {p}RB_Actual_Start_Date__c >= {start.isoformat()} AND {p}RB_Actual_Start_Date__c <= {today.isoformat()}")
    shifts_raw = sf.query("shifts", "SELECT Id, Name, RB_Customer__r.Name, RB_Actual_Start_Date__c, RB_Actual_Start_Time__c, RB_Actual_End_Time__c, "
        "RB_Customer_Store__r.Name, State__c, Employee_Name__r.FirstName, Employee_Name__r.LastName, Shift_Duration_Hours__c, "
        f"Demos_and_Tastings__c, Product_Sales__c, Total_Sales__c, Status__c, CreatedDate, RB_Comments__c FROM Timesheet__c WHERE {F()}")
    prods_raw = sf.query("products", f"SELECT Timesheet__r.Name, RB_Customer_Product__r.Name, Quantity__c FROM Product_Sale__c WHERE {F('Timesheet__r.')}")
    qa_raw = sf.query("questionnaire", f"SELECT RB_Timesheet__r.Name, RB_Question__c, RB_Answer__c FROM RB_Questionnaire__c WHERE {F('RB_Timesheet__r.')} AND RB_Is_Deleted__c = false")
    files_raw = sf.query("files", f"SELECT LinkedEntityId, ContentDocument.FileExtension FROM ContentDocumentLink WHERE LinkedEntityId IN (SELECT Id FROM Timesheet__c WHERE {F()})")

    # prices: live kitchenaid.com.au catalogue each run, saved copy as fallback
    live = None if a.fixtures else fetch_catalogue()
    cat_rows = live or list(csv.DictReader(open(ROOT / "pipeline" / "catalogue_au.tsv", encoding="utf-8"), delimiter="|"))
    cat_rows = [r for r in cat_rows if r["sku"]]
    # price history (RRP per product with the date each price started) travels inside the encrypted files,
    # so each sale keeps the RRP of its own date. Data file first, checks file as backup if one password changed.
    cpw = os.environ.get("CHECKS_PASSWORD") or ("test-checks-password" if a.fixtures else None)
    hist = None
    for f, k in ((Path(a.out), pw), (Path(a.out).with_name("checks.enc.json"), cpw)):
        if hist is None and f.exists():
            try: hist = (unseal(json.loads(f.read_text()), k) or {}).get("price_history")
            except Exception: hist = None
    if not hist:
        hist = seed_history(csv.DictReader(open(ROOT / "pipeline" / "catalogue_au.tsv", encoding="utf-8"), delimiter="|"), CFG["prices_checked"])
        if not a.fixtures: warn(f"Price history started from the saved catalogue ({CFG['prices_checked']}): sales up to then use those prices; later RRP changes are dated as they're seen.")
    if live:
        for sku, title, o, n in update_history(hist, live, today):
            warn(f"RRP changed on kitchenaid.com.au: {title} ({sku}) ${o:,.2f} → ${n:,.2f}. Sales from {today:%-d %b} use the new price; earlier sales keep the old one.")
    CFG["prices_checked"] = hist["checked"]
    pmap = {r["sf_product"]: r for r in csv.DictReader(open(ROOT / "pipeline" / "price_map_au.csv", encoding="utf-8"), delimiter="|")}
    cat = {r["sku"].upper(): r for r in cat_rows}
    priced = set(CFG.get("priced_countries", ["AU"]))
    auto_seen = {}
    def price(name, country, day):
        if country not in priced: return {"sku": "", "rrp": 0.0, "title": name, "match": "unpriced", "note": "No price list for this country yet"}
        if name in pmap:
            r = pmap[name]; rrp = rrp_at(hist, r["sku"], day)     # website price on the sale date; mapped RRP only if the SKU was never on the site
            if rrp is None: rrp = float(r["rrp"])
            return {"sku": r["sku"], "rrp": rrp, "title": r["title"], "match": r["match"], "note": r["note"]}
        m = re.match(r"^\s*(5?K[A-Z]{1,4}\d{2,5}[A-Z0-9]*)\s*-", name)
        if m and m.group(1).upper() in cat:
            c = cat[m.group(1).upper()]; return {"sku": c["sku"], "rrp": rrp_at(hist, c["sku"], day) or rrp_of(c), "title": c["title"], "match": "sku", "note": ""}
        if name not in auto_seen:
            auto_seen[name] = auto_match(name, cat_rows)
            if auto_seen[name]:
                c, loose = auto_seen[name]
                warn(f"Priced automatically from kitchenaid.com.au: '{name}' → {c['title']} {c['variant']} ({c['sku']}) ${rrp_of(c):,.2f}"
                     + (" — loose match, shown as estimate" if loose else "") + ". If wrong, add the right SKU to pipeline/price_map_au.csv.")
        if auto_seen[name]:
            c, loose = auto_seen[name]
            return {"sku": c["sku"], "rrp": rrp_at(hist, c["sku"], day) or rrp_of(c), "title": c["title"], "match": "estimated" if loose else "auto",
                    "note": "Matched automatically on kitchenaid.com.au" + (" (closest model, estimate)" if loose else "")}
        return None

    if not live and not a.fixtures: warn("Couldn't read kitchenaid.com.au this run — prices come from the saved catalogue (pipeline/catalogue_au.tsv).")
    pc = CFG.get("prices_checked")
    if pc and (today - dt.date.fromisoformat(pc)).days > CFG.get("prices_max_age_days", 30):
        warn(f"Prices are stale: catalogue last checked {pc} ({(today - dt.date.fromisoformat(pc)).days} days ago). Refresh pipeline/catalogue_au.tsv from kitchenaid.com.au and update prices_checked in config.json.")

    prods = defaultdict(list)
    for r in prods_raw: prods[r["Timesheet__r"]["Name"]].append((r["RB_Customer_Product__r"]["Name"], int(r.get("Quantity__c") or 1)))
    qn = {q["n"]: q["key"] for q in CFG["questions"] if "n" in q}
    answers = defaultdict(dict)
    for r in qa_raw:
        m = re.match(r"\s*(\d+)\.", r["RB_Question__c"] or "")
        if m and m.group(1) in qn: answers[r["RB_Timesheet__r"]["Name"]][qn[m.group(1)]] = (r["RB_Answer__c"] or "").strip()
    files = Counter(r["LinkedEntityId"] for r in files_raw)
    learn_names([r["RB_Answer__c"] for r in qa_raw] + [s.get("RB_Comments__c") for s in shifts_raw])
    if not NAMES: warn("The REDACT_NAMES secret is empty: only names found next to a role or after 'customer' are removed automatically.")
    if LEARNED: warn("Names removed automatically this run: " + ", ".join(f"{k} → {v}" for k, v in sorted(LEARNED.items())))

    # statuses, drafts, duplicates
    keep = []
    for s in shifts_raw:
        st = s["Status__c"]
        if st == "Draft": warn(f"Shift {s['Name']} ({s['RB_Actual_Start_Date__c']}, {s['RB_Customer_Store__r']['Name']}) is a Draft — shown on the dashboard as Draft.")
        elif st not in ("Shift Submitted", "Shift Validated", "Approved"): warn(f"Shift {s['Name']} has status '{st}' — left out."); continue
        if s["RB_Customer_Store__r"]["Name"] in CFG["exclude_stores"]: continue
        keep.append(s)
    groups = defaultdict(list)
    for s in keep:
        groups[(s["Employee_Name__r"]["FirstName"], s["Employee_Name__r"]["LastName"], s["RB_Customer_Store__r"]["Name"], s["RB_Actual_Start_Date__c"])].append(s)
    dup_out = set()
    hm = lambda x: int((x or "00:00")[:2]) * 60 + int((x or "00:00")[3:5])
    overlap = lambda a, b: hm(a["RB_Actual_Start_Time__c"]) < hm(b["RB_Actual_End_Time__c"]) and hm(b["RB_Actual_Start_Time__c"]) < hm(a["RB_Actual_End_Time__c"])
    for g in groups.values():
        if len(g) > 1:
            g.sort(key=lambda x: x["CreatedDate"]); kept = [g[0]]
            for extra in g[1:]:
                twin = next((k for k in kept if overlap(k, extra)), None)
                if twin is None: kept.append(extra); continue
                dup_out.add(extra["Name"]); warn(f"Possible duplicate: shift {extra['Name']} overlaps {twin['Name']} (same person, store, date and hours) — later one left out. Check before invoicing.")

    rows = []
    for s in keep:
        if s["Name"] in dup_out: continue
        sid = s["Name"]; country = CFG["accounts"][s["RB_Customer__r"]["Name"]]; d = dt.date.fromisoformat(s["RB_Actual_Start_Date__c"])
        plist, est, missing = [], False, False
        for name, q in prods.get(sid, []):
            p = price(name, country, d)
            if p is None:
                missing = True
                if country == "AU": warn(f"No price for '{name}' (shift {sid}) — value excludes it. Add it to pipeline/price_map_au.csv.")
                p = {"sku": "", "rrp": 0.0, "title": name, "match": "missing", "note": "No price found"}
            est |= p["match"] == "estimated"
            plist.append({"name": clean_product_name(name), "sku": p["sku"], "q": q, "rrp": p["rrp"], "value": round(p["rrp"] * q, 2),
                          "model": model_of(p["title"] + " " + name), "cat": category(p["title"] + " " + name), "match": p["match"], "note": p["note"]})
        units = sum(p["q"] for p in plist)
        if units != int(s["Product_Sales__c"] or 0): warn(f"Shift {sid}: {units} product lines but Product_Sales = {s['Product_Sales__c']}.")
        ans = {q["key"]: None for q in CFG["questions"]}
        for k, v in answers.get(sid, {}).items(): ans[k] = redact(v, sid)
        if not answers.get(sid): warn(f"Shift {sid}: no questionnaire answers.")
        fn = first_num(ans.get("fridge"))
        if fn is not None and fn > 5: warn(f"HACCP: shift {sid} logged fridge temperature {fn} °C (limit 5 °C).")
        if ans.get("milk_ok") and re.search(r"\b(no|not|damaged|warm|expired|dirty)\b", ans["milk_ok"], re.I): warn(f"HACCP: shift {sid} milk/equipment answer: {ans['milk_ok'][:120]}")
        for q in CFG["questions"]:
            if q.get("internal"): ans.pop(q["key"], None)          # HACCP answers stay in checks only, never on the client dashboard
            elif q.get("source") == "timesheet": ans[q["key"]] = redact((s.get(q["field"]) or "").strip(), sid)
        where = f"{sid} ({d.isoformat()}, {title_store(s['RB_Customer_Store__r']['Name'])})"
        for v in ans.values(): review(v, sid, where)
        t = lambda x: (x or "")[:5]
        rows.append({"id": sid, "date": d.isoformat(), "week": monday(d).isoformat(), "state": s["State__c"], "store": title_store(s["RB_Customer_Store__r"]["Name"]),
            "retailer": retailer(s["RB_Customer_Store__r"]["Name"]), "staff": staff_name(s["Employee_Name__r"]["FirstName"], s["Employee_Name__r"]["LastName"]),
            "start": t(s["RB_Actual_Start_Time__c"]), "end": t(s["RB_Actual_End_Time__c"]), "hours": float(s["Shift_Duration_Hours__c"] or 0), "demos": int(s["Demos_and_Tastings__c"] or 0),
            "units": units, "unlisted": max(0, int(s["Total_Sales__c"] or 0) - int(s["Product_Sales__c"] or 0)),
            "status": {"Draft": "Draft", "Shift Submitted": "Pending approval"}.get(s["Status__c"], "Approved"),
            "products": plist, "value": round(sum(p["value"] for p in plist), 2), "estimated": est, "price_missing": missing,
            "answers": ans, "visitors_n": first_num(ans.get("visitors")), "traffic_level": traffic(ans.get("traffic")),
            "country": country, "currency": "NZD" if country == "NZ" else "AUD",
            "photos": {"count": files.get(s["Id"], 0), "link": f"https://meshcircle.my.site.com/customer/s/relatedlist/{s['Id'][:15]}/AttachedContentDocuments"}})
    rows.sort(key=lambda r: (r["date"], r["country"], r["state"], r["store"]))
    data = {"client": CFG["client"], "program": CFG["program"], "generated_at": now.isoformat(timespec="minutes"), "data_as_at": today.isoformat(),
            "period_label": f"from {start.day} {start.strftime('%b')}", "window": [start.isoformat(), today.isoformat()],
            "questions": [{k: q[k] for k in ("key", "label", "full")} for q in CFG["questions"] if not q.get("internal")], "models": CFG["models"],
            "price_source": CFG["price_source"].replace("{checked}", dt.date.fromisoformat(CFG["prices_checked"]).strftime("%-d %b %Y")), "shifts": rows, "price_history": hist,
            "priced_currencies": sorted({"NZ": "NZD"}.get(c, "AUD") for c in priced)}
    # Refresh button: a narrow GitHub token (Actions write, this repo only) travels INSIDE the encrypted file,
    # so only people with the dashboard password can use it to start a refresh.
    if os.environ.get("DISPATCH_TOKEN") and not a.fixtures:
        repo = os.environ.get("GITHUB_REPOSITORY", "mesh-marketing/kitchenaid-demo-dashboard")
        ok, exp = token_health(os.environ["DISPATCH_TOKEN"], repo, today)
        if ok: data["dispatch"] = {"token": os.environ["DISPATCH_TOKEN"], "repo": repo}   # an expired token is left out, so Refresh falls back to the saved data
        if exp: public_note.append(exp)

    def seal(obj, password):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        salt, iv, it = os.urandom(16), os.urandom(12), 600_000
        key = PBKDF2HMAC(hashes.SHA256(), 32, salt, it).derive(password.encode())
        ct = AESGCM(key).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode(), None)
        b = lambda x: base64.b64encode(x).decode()
        return {"v": 1, "s": b(salt), "i": b(iv), "n": it, "c": b(ct)}
    def opens(blob, password):
        """True if the published file decrypts with the current password (so a password change forces a re-encrypt)."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        from cryptography.hazmat.primitives import hashes
        try:
            d = lambda k: base64.b64decode(blob[k])
            key = PBKDF2HMAC(hashes.SHA256(), 32, d("s"), int(blob["n"])).derive(password.encode())
            AESGCM(key).decrypt(d("i"), d("c"), None); return True
        except Exception:
            return False
    def write_checks():
        if not cpw: return
        body = seal({"generated_at": now.isoformat(timespec="minutes"), "window": [start.isoformat(), today.isoformat()], "checks": WARN, "price_history": hist}, cpw)
        body["g"] = now.isoformat(timespec="minutes"); body["k"] = len(WARN)
        chk = Path(a.out).with_name("checks.enc.json")
        try:
            prev = json.loads(chk.read_text()) if chk.exists() else {}
        except Exception:
            prev = {}
        import hashlib
        hw = hashlib.sha256(("\n".join(WARN) + json.dumps(hist, sort_keys=True)).encode()).hexdigest()
        if prev.get("h") == hw and opens(prev, cpw): return            # same checks as last time: don't republish
        body["h"] = hw; chk.write_text(json.dumps(body))
    def write_status():
        # public, unencrypted: only the time of the last successful Salesforce check (no client data)
        Path(a.out).with_name("status.json").write_text(json.dumps({"checked": now.isoformat(timespec="minutes")}))
    def public_log(msg):
        for extra in public_note: msg += " " + extra
        print(msg)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f: f.write(msg + "\n")

    # skip publishing if nothing changed (keeps the repo small; "Data updated" = last real change)
    import hashlib
    digest = hashlib.sha256(json.dumps({k: v for k, v in data.items() if k not in ("generated_at", "data_as_at")}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    outp = Path(a.out)
    if outp.exists():
        try:
            old = json.loads(outp.read_text())
            if old.get("h") == digest and opens(old, pw):
                write_checks(); write_status(); public_log(f"No data change. {len(WARN)} internal check(s) — see checks.html.")
                return
        except Exception:
            pass

    # encrypt
    blob = seal(data, pw); blob.update({"g": data["generated_at"], "h": digest})
    outp.write_text(json.dumps(blob))
    write_checks(); write_status()
    if a.plain: Path(a.plain).write_text(json.dumps(data, ensure_ascii=False, indent=1))

    public_log(f"Data refreshed {data['generated_at']}. {len(WARN)} internal check(s) — see checks.html.")   # no client data in public logs
    if a.fixtures:  # local test only
        print(f"[local] {len(rows)} shifts, {sum(r['demos'] for r in rows)} demos, {sum(r['units'] for r in rows)} units, {sum(r['photos']['count'] for r in rows)} photos")
        print("[local] checks:", *WARN, sep="\n  - ")

if __name__ == "__main__":
    main()
