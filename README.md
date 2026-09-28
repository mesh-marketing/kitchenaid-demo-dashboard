# KitchenAid coffee demo dashboard (live)

Weekly in-store demo dashboard for KitchenAid AU & NZ, refreshed automatically from Mesh Circle (Salesforce).

- **Dashboard:** `index.html`. Client password. Checks for new data every 5 minutes; **↻ Refresh** loads the newest data straight away, keeping your filters.
- **Checks:** `checks.html`. Mesh-only password. Lists drafts, possible duplicates, missing prices, HACCP readings and redactions.
- **Data:** `data.enc.json` and `checks.enc.json` are AES-256 encrypted. The repo and site are public, but nothing readable is published, and the job logs print no client data.

## How it refreshes
`.github/workflows/refresh.yml` runs every 15 minutes, 6am–9pm Sydney time. It pulls Salesforce, applies the rules below, and commits new encrypted files only when the data has changed. GitHub Pages republishes within about a minute. Each run also writes `status.json` (the time of the last successful check, nothing else), shown top right as "Checked with Salesforce". GitHub can delay scheduled runs when busy, so gaps of 30–60 minutes can happen. To refresh right now, open **Actions → Refresh dashboard data → Run workflow** (checks.html links there).

Rules, identical to the approved manual build:
- Statuses: Submitted (shown as pending approval), Validated and Approved are included. Drafts are left out.
- Possible duplicates (same person, store, date and overlapping hours): the later entry is left out and listed in checks.
- Value at RRP comes from `pipeline/price_map_au.csv` and `catalogue_au.tsv` (kitchenaid.com.au, checked 28 Sep 2026). Unknown products are valued at 0 and listed in checks.
- Answers: questions 1–7 plus the timesheet "Comments." field. HACCP answers (fridge temperature, milk & equipment) are not shown to the client; any issue is listed in checks.html.
- Health details (and any retailer staff names set in the `REDACT_NAMES` secret) are removed automatically. Mesh still reviews checks.html.
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

## Maintenance
- New product with no price: it appears in checks. Add a line to `price_map_au.csv`.
- Change a password: update the secret, then run the workflow. The files are re-encrypted with the new password on that run, and the old one stops working.
- Scheduled workflows pause after 60 days without any repo activity. The data commits normally count as activity; if demos stop for two months, re-enable the workflow in Actions.
- Never commit readable data. `tests/` and `*.plain.json` are gitignored for that reason.
