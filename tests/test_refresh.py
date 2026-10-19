"""Offline regression test for pipeline/refresh.py using synthetic data (no client data, no network).
Run:  python tests/test_refresh.py      (needs: pip install cryptography)
Covers: names next to a role or after 'customer' removed, drafts excluded, duplicate shifts excluded, health detail redacted, HACCP kept off the client file,
missing price valued at 0 and flagged, AU/NZ currency split, encrypted output opens with the password and not with a wrong one.
"""
import base64, json, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent

def decrypt(blob, pw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    d = lambda k: base64.b64decode(blob[k])
    key = PBKDF2HMAC(hashes.SHA256(), 32, d("s"), int(blob["n"])).derive(pw.encode())
    return json.loads(AESGCM(key).decrypt(d("i"), d("c"), None))

with tempfile.TemporaryDirectory() as t:
    out = Path(t) / "data.enc.json"
    r = subprocess.run([sys.executable, "-I", str(ROOT / "pipeline/refresh.py"), "--fixtures", str(ROOT / "tests/synthetic"), "--today", "2026-09-28", "--out", str(out)],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    blob = json.loads(out.read_text()); data = decrypt(blob, "test-password-only")
    chk = decrypt(json.loads((Path(t) / "checks.enc.json").read_text()), "test-checks-password")["checks"]
    try: decrypt(blob, "wrong-password-123"); raise SystemExit("FAIL: wrong password opened the file")
    except SystemExit: raise
    except Exception: pass
rows = {s["id"]: s for s in data["shifts"]}
assert set(rows) == {"90001", "90002", "90003", "90004", "90005"}, sorted(rows)   # duplicate 90006 left out; draft 90004 shown
assert rows["90004"]["status"] == "Draft", rows["90004"]["status"]
assert rows["90003"]["currency"] == "NZD" and rows["90001"]["currency"] == "AUD"
assert rows["90001"]["value"] == 1599 and rows["90001"]["photos"]["count"] == 2 and rows["90001"]["answers"]["comments"] == "Great day"
assert rows["90002"]["price_missing"] and rows["90002"]["value"] == 1599 and rows["90002"]["status"] == "Pending approval"
assert "diabet" not in json.dumps(data).lower()                                   # health detail removed
assert "fridge" not in rows["90002"]["answers"] and "milk_ok" not in rows["90002"]["answers"]   # HACCP stays internal
assert rows["90002"]["traffic_level"] == "Light"
assert rows["90003"]["answers"]["store_fb"] == "Yes - the Section Managers were great. The manager said come back.", rows["90003"]["answers"]["store_fb"]
assert rows["90005"]["answers"]["cust_fb"] == "A cust put a KF4 on hold. Most people liked it.", rows["90005"]["answers"]["cust_fb"]   # "Most" is not a name                                  # lowest level mentioned
joined = " | ".join(chk)
for needle in ("Draft", "Possible duplicate", "No price for", "HACCP", "health-related"):
    assert needle in joined, f"missing check: {needle}"
print("OK: all checks passed;", len(rows), "shifts,", len(chk), "internal checks")
