"""Refresh the KitchenAid demo dashboard data from Salesforce.

Runs in GitHub Actions (see .github/workflows/refresh.yml) or locally:
  SF_DOMAIN=meshcircle.my.salesforce.com SF_CLIENT_ID=... SF_CLIENT_SECRET=... DASHBOARD_PASSWORD=... python pipeline/refresh.py
  python pipeline/refresh.py --fixtures tests/fixtures --today 2026-09-28      (offline test with recorded responses)

Writes data.enc.json (AES-256-GCM, key from the dashboard password). Nothing readable is published.
Internal warnings (drafts, duplicates, redactions, missing prices, HACCP) go to the GitHub job summary, never to the site.
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

def redact(text, sid):
    if not text: return text
    out = text
    for name, role in sorted(NAMES.items(), key=lambda kv: -len(kv[0])):
        out = re.sub(rf"\b{re.escape(name)}(?![\w])", role, out)
    out = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[removed]", out)
    out = re.sub(r"\b(?:\+?61|0)[2-478](?:[ -]?\d){8}\b", "[removed]", out)
    for term in CFG["redact"]["health_terms"]:
        if term in out.lower():
            new = re.sub(rf"\s*\([^)]*{term}[^)]*\)", "", out, flags=re.I)
            if new == out:  # not in brackets: drop the sentence
                new = re.sub(rf"[^.!?\n]*{term}[^.!?\n]*[.!?]?", "[detail removed]", out, flags=re.I)
            out = new; warn(f"Shift {sid}: removed a health-related detail from an answer — check it reads correctly.")
    return out

# ---------------- main ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures"); ap.add_argument("--today"); ap.add_argument("--out", default=str(ROOT / "data.enc.json"))
    ap.add_argument("--plain", help="also write the unencrypted JSON here (local testing only, never commit)")
    a = ap.parse_args()
    tz = ZoneInfo(CFG["timezone"]); now = dt.datetime.now(tz)
    today = dt.date.fromisoformat(a.today) if a.today else now.date()
    start = monday(today) - dt.timedelta(weeks=CFG["weeks_back"] - 1)
    sf = Fixtures(a.fixtures) if a.fixtures else Salesforce(os.environ["SF_DOMAIN"], os.environ["SF_CLIENT_ID"], os.environ["SF_CLIENT_SECRET"])
    pw = os.environ.get("DASHBOARD_PASSWORD") or ("test-password-only" if a.fixtures else None)
    if not pw or len(pw) < 14: raise SystemExit("DASHBOARD_PASSWORD missing or too short (min 14 characters).")

    accts = ",".join(f"'{x}'" for x in CFG["accounts"])
    F = lambda p="": (f"{p}RB_Customer__r.Name IN ({accts}) AND {p}Business_Unit_Text__c = '{CFG['business_unit']}' "
                      f"AND {p}RB_Actual_Start_Date__c >= {start.isoformat()} AND {p}RB_Actual_Start_Date__c <= {today.isoformat()}")
    shifts_raw = sf.query("shifts", "SELECT Id, Name, RB_Customer__r.Name, RB_Actual_Start_Date__c, RB_Actual_Start_Time__c, RB_Actual_End_Time__c, "
        "RB_Customer_Store__r.Name, State__c, Employee_Name__r.FirstName, Employee_Name__r.LastName, Shift_Duration_Hours__c, "
        f"Demos_and_Tastings__c, Product_Sales__c, Total_Sales__c, Status__c, CreatedDate FROM Timesheet__c WHERE {F()}")
    prods_raw = sf.query("products", f"SELECT Timesheet__r.Name, RB_Customer_Product__r.Name, Quantity__c FROM Product_Sale__c WHERE {F('Timesheet__r.')}")
    qa_raw = sf.query("questionnaire", f"SELECT RB_Timesheet__r.Name, RB_Question__c, RB_Answer__c FROM RB_Questionnaire__c WHERE {F('RB_Timesheet__r.')} AND RB_Is_Deleted__c = false")
    files_raw = sf.query("files", f"SELECT LinkedEntityId, ContentDocument.FileExtension FROM ContentDocumentLink WHERE LinkedEntityId IN (SELECT Id FROM Timesheet__c WHERE {F()})")

    # prices
    pmap = {r["sf_product"]: r for r in csv.DictReader(open(ROOT / "pipeline" / "price_map_au.csv", encoding="utf-8"), delimiter="|")}
    cat = {r["sku"].upper(): r for r in csv.DictReader(open(ROOT / "pipeline" / "catalogue_au.tsv", encoding="utf-8"), delimiter="|") if r["sku"]}
    def price(name, country):
        if country != "AU": return None
        if name in pmap: r = pmap[name]; return {"sku": r["sku"], "rrp": float(r["rrp"]), "title": r["title"], "match": r["match"], "note": r["note"]}
        m = re.match(r"^\s*(5?K[A-Z]{1,4}\d{2,5}[A-Z0-9]*)\s*-", name)
        if m and m.group(1).upper() in cat:
            c = cat[m.group(1).upper()]; return {"sku": c["sku"], "rrp": float(c["compare_at"] or c["price"]), "title": c["title"], "match": "sku", "note": ""}
        return None

    prods = defaultdict(list)
    for r in prods_raw: prods[r["Timesheet__r"]["Name"]].append((r["RB_Customer_Product__r"]["Name"], int(r.get("Quantity__c") or 1)))
    qn = {q["n"]: q["key"] for q in CFG["questions"]}
    answers = defaultdict(dict)
    for r in qa_raw:
        m = re.match(r"\s*(\d+)\.", r["RB_Question__c"] or "")
        if m and m.group(1) in qn: answers[r["RB_Timesheet__r"]["Name"]][qn[m.group(1)]] = (r["RB_Answer__c"] or "").strip()
    files = Counter(r["LinkedEntityId"] for r in files_raw)

    # statuses, drafts, duplicates
    keep = []
    for s in shifts_raw:
        st = s["Status__c"]
        if st == "Draft": warn(f"Shift {s['Name']} ({s['RB_Actual_Start_Date__c']}, {s['RB_Customer_Store__r']['Name']}) is a Draft — left out until submitted."); continue
        if st not in ("Shift Submitted", "Shift Validated", "Approved"): warn(f"Shift {s['Name']} has status '{st}' — left out."); continue
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
            p = price(name, country)
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
        t = lambda x: (x or "")[:5]
        rows.append({"id": sid, "date": d.isoformat(), "week": monday(d).isoformat(), "state": s["State__c"], "store": title_store(s["RB_Customer_Store__r"]["Name"]),
            "retailer": retailer(s["RB_Customer_Store__r"]["Name"]), "staff": staff_name(s["Employee_Name__r"]["FirstName"], s["Employee_Name__r"]["LastName"]),
            "start": t(s["RB_Actual_Start_Time__c"]), "end": t(s["RB_Actual_End_Time__c"]), "hours": float(s["Shift_Duration_Hours__c"] or 0), "demos": int(s["Demos_and_Tastings__c"] or 0),
            "units": units, "unlisted": max(0, int(s["Total_Sales__c"] or 0) - int(s["Product_Sales__c"] or 0)),
            "status": "Pending approval" if s["Status__c"] == "Shift Submitted" else "Approved",
            "products": plist, "value": round(sum(p["value"] for p in plist), 2), "estimated": est, "price_missing": missing,
            "answers": ans, "visitors_n": first_num(ans.get("visitors")), "traffic_level": traffic(ans.get("traffic")), "fridge_n": fn,
            "country": country, "currency": "NZD" if country == "NZ" else "AUD",
            "photos": {"count": files.get(s["Id"], 0), "link": f"https://meshcircle.my.site.com/customer/s/relatedlist/{s['Id'][:15]}/AttachedContentDocuments"}})
    rows.sort(key=lambda r: (r["date"], r["country"], r["state"], r["store"]))
    data = {"client": CFG["client"], "program": CFG["program"], "generated_at": now.isoformat(timespec="minutes"), "data_as_at": today.isoformat(),
            "period_label": f"from {start.day} {start.strftime('%b')}", "window": [start.isoformat(), today.isoformat()],
            "questions": [{k: q[k] for k in ("key", "label", "full")} for q in CFG["questions"]], "models": CFG["models"],
            "price_source": CFG["price_source"], "shifts": rows}

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
    cpw = os.environ.get("CHECKS_PASSWORD") or ("test-checks-password" if a.fixtures else None)
    def write_checks():
        if not cpw: return
        body = seal({"generated_at": now.isoformat(timespec="minutes"), "window": [start.isoformat(), today.isoformat()], "checks": WARN}, cpw)
        body["g"] = now.isoformat(timespec="minutes"); body["k"] = len(WARN)
        chk = Path(a.out).with_name("checks.enc.json")
        try:
            prev = json.loads(chk.read_text()) if chk.exists() else {}
        except Exception:
            prev = {}
        import hashlib
        hw = hashlib.sha256("\n".join(WARN).encode()).hexdigest()
        if prev.get("h") == hw and opens(prev, cpw): return            # same checks as last time: don't republish
        body["h"] = hw; chk.write_text(json.dumps(body))
    def public_log(msg):
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
                write_checks(); public_log(f"No data change. {len(WARN)} internal check(s) — see checks.html.")
                return
        except Exception:
            pass

    # encrypt
    blob = seal(data, pw); blob.update({"g": data["generated_at"], "h": digest})
    outp.write_text(json.dumps(blob))
    write_checks()
    if a.plain: Path(a.plain).write_text(json.dumps(data, ensure_ascii=False, indent=1))

    public_log(f"Data refreshed {data['generated_at']}. {len(WARN)} internal check(s) — see checks.html.")   # no client data in public logs
    if a.fixtures:  # local test only
        print(f"[local] {len(rows)} shifts, {sum(r['demos'] for r in rows)} demos, {sum(r['units'] for r in rows)} units, {sum(r['photos']['count'] for r in rows)} photos")
        print("[local] checks:", *WARN, sep="\n  - ")

if __name__ == "__main__":
    main()
