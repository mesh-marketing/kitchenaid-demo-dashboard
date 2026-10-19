# KitchenAid coffee demo dashboard (live)

Weekly in-store demo dashboard for KitchenAid AU & NZ, refreshed automatically from Mesh Circle (Salesforce).

- **Dashboard:** `index.html`. Client password. Checks for new data every 5 minutes; **↻ Refresh** loads the newest data straight away, keeping your filters.
- **Checks:** `checks.html`. Mesh-only password. Lists drafts, possible duplicates, missing prices, HACCP readings and redactions.
- **Data:** `data.enc.json` and `checks.enc.json` are AES-256 encrypted. The repo and site are public, but nothing readable is published, and the job logs print no client data.

## How it refreshes
`.github/workflows/refresh.yml` runs every 15 minutes, 6am–9pm Sydney time. It pulls Salesforce, applies the rules below, and commits new encrypted files only when the data has changed. GitHub Pages republishes within about a minute. Each run also writes `status.json` (the time of the last successful check, nothing else), shown top right as "Checked with Salesforce". GitHub can delay scheduled runs when busy, so gaps of 30–60 minutes can happen. To refresh right now, open **Actions → Refresh dashboard data → Run workflow** (checks.html links there).

Rules, identical to the approved manual build:
- Statuses: Submitted (shown as pending approval), Validated and Approved are included. Drafts are shown with a "Draft" status and a note that figures may be incomplete.
- Possible duplicates (same person, store, date and overlapping hours): the later entry is left out and listed in checks.
- Value at RRP: every refresh reads the live price lists from kitchenaid.com.au (AU, A$) and kitchenaid.co.nz (NZ, NZ$); NZ uses the same SKU mapping with NZ prices, and its history starts from the first day it was read (19 Oct 2026). AU details: (RRP = the 'was' price during promotions, else the price). Each sale is valued at the RRP in force on its shift date: a price history (inside the encrypted data and checks files) records every RRP change from the day it's first seen, and checks lists each change. Sales before 28 Sep 2026 use the 28 Sep price list. If both passwords are changed at once the history restarts from `catalogue_au.tsv`, so change one, run a refresh, then the other. Products in `pipeline/price_map_au.csv` use their mapped SKU. Any other product is matched to the website by name (model, type, colour) and listed in checks as "Priced automatically"; loose matches show as estimates. Products it can't match confidently are valued at 0 and listed in checks — never guessed. If the website can't be reached, the saved `catalogue_au.tsv` is used and checks says so.
- Answers: questions 1–7 plus the timesheet "Comments." field. HACCP answers (fridge temperature, milk & equipment) are not shown to the client; any issue is listed in checks.html.
- Names: retailer staff named next to their role ("Zelda (Section Manager)") and customers named after "customer"/"cust" are replaced automatically everywhere ("the manager", "the customer"). Other names go in the `REDACT_NAMES` secret. Any first name still visible is listed on checks.html as "Possible person's name".
- Health details, emails and phone numbers are removed automatically.
- Answers that may need a look before the client sees them (lateness, sick staff, complaints, faulty stock, remarks about ethnicity) are listed on checks.html as "Review before sharing". They are not changed automatically.
- AU and NZ values are kept separate (A$ and NZ$).

## One-time setup (about 20 minutes)
**1. Salesforce (admin).** Create an External Client App called "GitHub Dashboard Refresh":
   - OAuth enabled, scope `api`. Enable **Client Credentials Flow** and set **Run As** to an integration user.
   - Give that user a **read-only** permission set covering Timesheet__c, Product_Sale__c, RB_Questionnaire__c, RB_Customer_Product__c, RB_Customer_Store__c, Account, Contact, and access to files on timesheets.
   - Copy the Consumer Key and Secret.

**2. GitHub (repo settings).** Under Secrets and variables → Actions, add:
   - `SF_DOMAIN` = `meshcircle.my.salesforce.com`
   - `SF_CLIENT_ID`, `SF_CLIENT_SECRET` (from step 1)
   - `DASHBOARD_PASSWORD` (client password, 16+ characters)
   - `CHECKS_PASSWORD` (Mesh only, different from the client password)
   - `REDACT_NAMES` (JSON of retailer staff names to replace with their role, e.g. `{"Sam": "the manager"}`; kept secret so no names are public)

**3. Pages.** Under Settings → Pages, choose Deploy from branch → `main` / root. The link will be `https://mesh-marketing.github.io/<repo-name>/`.

**3b. Refresh button (optional).** Create a fine-grained GitHub token (this repo only; Actions: read and write, Contents: read-only), add it as the secret `DISPATCH_TOKEN`, and add `DISPATCH_TOKEN: ${{ secrets.DISPATCH_TOKEN }}` to the env list in `refresh.yml`. The token is stored only inside the encrypted data, so only password holders can use it; the button then pulls Salesforce on demand (about a minute). Renew the token before it expires.

**4. First run.** Open Actions → Refresh dashboard data → Run workflow, and check that it goes green.

## Maintenance (weekly, 5 minutes)
Open checks.html each Monday. Act on:
- **Possible person's name:** add it to the `REDACT_NAMES` secret (JSON, e.g. `{"Zelda": "a staff member"}`) and run the workflow.
- **Priced automatically …:** check the match. If it's wrong, add a line to `pipeline/price_map_au.csv` with the right SKU.
- **No price for …:** no confident match on the website (or not sold there). Add a line to `pipeline/price_map_au.csv`.
- **Couldn't read kitchenaid.com.au / Prices are stale:** only appears if the website has been unreachable from GitHub. Usually clears on the next run; if it persists, ask Claude to check.
- **DISPATCH_TOKEN expires in N days:** create a new token (same settings) and update the secret.
- **Review before sharing / HACCP:** follow up with the demonstrator; edit the answer in Salesforce if it shouldn't be client-facing.
- **A refresh failure** opens a GitHub issue (once the proposed workflow is in), and the dashboard shows a red "may be out of date" banner if no check has succeeded for 2 hours in the daytime.

Before changing `pipeline/refresh.py`, run `python tests/test_refresh.py` (synthetic data only, no client data, no network).

## Other notes
- New product with no price: it appears in checks. Add a line to `price_map_au.csv`.
- Change a password: update the secret, then run the workflow. The files are re-encrypted with the new password on that run, and the old one stops working.
- Scheduled workflows pause after 60 days without any repo activity. The data commits normally count as activity; if demos stop for two months, re-enable the workflow in Actions.
- Never commit readable data. `tests/` and `*.plain.json` are gitignored for that reason.
