# Track — private, mobile-first portfolio website

A working FastAPI + HTML/CSS/JavaScript replacement frontend for your uploaded Streamlit portfolio tracker. Six tabs: Home, Analytics, New Trade, Trade History, Tax, Account. No Streamlit runtime or Node.js build step required.

**Your private uploaded 1,554-trade CSV is intentionally NOT bundled with this project.** Log in, open **Account → Upload / replace portfolio**, and upload your original CSV. The import/export format is compatible with your Streamlit app: semicolon-delimited, with the same 13 columns.

## Features and behavior

- **Home:** current base-currency market value, invested cost, unrealized P/L, a graph with **1M / 3M / 6M / 1Y / 3Y / MAX**, refresh button, and a scrollable list of open positions with ticker badges. The chart shows *portfolio account market value*, **not a contribution-adjusted investment return**. Deposits and sales change this line.
- **Analytics:** ECharts treemap by asset class → ticker, click to drill down; detailed holdings include quantity, last price, FIFO average, trade-currency unrealized P/L and current base-currency allocation percentage.
- **New Trade:** buy/sell, date, asset, Yahoo Finance ticker, type, quantity, unit price, trade currency, fees, STT, automatic date-specific FX or manual FX, and save.
- **Trade History:** searchable, incremental list, with edit and delete of individual transactions (including fee/FX fields).
- **Tax:** date presets/custom range, FIFO realized P/L in base or trade currency, and CSV export. As in the original Streamlit script, base-currency INR realized P/L **excludes STT** from both acquisition and disposal costs; reporting in trade currency excludes STT too. Totals are not summed across mixed trade currencies.
- **Account:** base currency, Google OAuth sign-in when configured, local username/password, hide balances, CSV import/export, sign out.
- **Privacy:** sessions use secure, HttpOnly signed cookies in production; trades are stored per authenticated user, not in the static frontend. HTTPS is automatic on Render. Never commit or distribute your real trades CSV.

## How it works

```
User's browser (HTML/CSS/JavaScript + ECharts)
   └─ same-origin HTTP API
        └─ FastAPI (app.py)
             ├─ portfolio.py  (CSV, FIFO, trade calculations)
             ├─ market.py     (NSE/Yahoo latest quotes, historical Yahoo prices + Frankfurter FX)
             └─ storage.py    (SQLite local / PostgreSQL hosted)
```

History is calculated with Pandas/NumPy cumulative position changes and array arithmetic across a daily calendar. Historical quote and FX responses are cached in the database for 18 hours; for every range change, only the selected period is requested and chart rendering is client-side. Changing between already-loaded date ranges is immediate. **The first load on a free, sleeping host and the first fetch of uncached external market data can still be slow.** Yahoo Finance is a best-effort public source and may rate-limit or fail; this is not suitable for guaranteed real-time prices.

For historical dates without market quotes, the backend uses the last observed transaction price as an **estimate**, and returns a warning and a list of estimated assets. If live or historical FX is unavailable, the app may fall back to a recorded transaction FX rate as an **explicitly labeled estimate**, never an arbitrary 1.0 for cross-currency conversion. Assets with no usable recorded FX are excluded and flagged. The portfolio chart is daily-close valuation and does **not** include unrecorded cash, dividends, stock-split/share-adjustment modeling, or external benchmark performance; these require additional transactions and corporate-action data. It should not be treated as a broker statement or certified tax calculation.

### Source-specific accounting details

The imported CSV holds the source's `fx_to_base` values as used by the original Streamlit app (whose default was SEK). When changing base currency in Account, historical transaction-date FX is retrieved, persisted into the CSV's `fx_to_base` column, and used for FIFO base-currency calculations. GBX (British pence) is normalized to GBP/100 for FX conversion. Market value is calculated with the latest stored trade-currency price multiplied by a *current* FX rate. Price refresh looks up on-demand latest traded quotes: `.NS` securities try the NSE India equity quote endpoint first, then Yahoo Finance 5-minute intraday quotes; other tickers use Yahoo intraday quotes. Failed lookups keep the previously stored LTP unchanged and are reported by ticker. Neither public endpoint has guaranteed uptime or a licensed real-time SLA. Historical charts still use daily closing prices, and estimates on dates without history are labeled separately from latest-price refresh results.

Your source CSV has one historical FIFO inconsistency: `ALV` includes an unmatched sale of about 0.056974 units. The original Streamlit engine skips unmatched quantities during realized P/L; this version preserves that matching behavior and emits an explicit warning. Review/correct that trade if needed.

## 1. Run locally (Windows, macOS or Linux)

1. Install Python 3.11 or newer and download/unzip this project.
2. In a terminal, change to the unzipped folder containing `app.py`.
3. Create a virtual environment and install dependencies:

```bash
python -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

4. Copy `.env.example` to `.env` and edit it. Set a random `SESSION_SECRET`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Paste the generated string into `SESSION_SECRET`. For local development leave `APP_ENV=development`, `DATABASE_URL=` (empty), and Google OAuth keys blank. The database will be `portfolio_local.db` in the project folder.

5. Start the server:

```bash
uvicorn app:app --reload --env-file .env
```

6. Visit **http://127.0.0.1:8000**. Create a local account with a password of at least 8 characters. Open **Account → Upload / replace portfolio** and choose the original trades CSV. The app does not contain your uploaded trades by default. For a synthetic trial, import the included `sample_trades.csv` instead; its numbers are fictitious.
7. In **Home**, tap the small refresh icon to request latest traded prices. Historical chart requests for 1M, 3M, etc. will use network APIs on first request.

**Note:** Google OAuth is optional. Local sign-in works immediately without any external identity provider.

## 2. Recommended free cloud deployment: Render + Supabase

Use **one free Render Python Web Service for the whole application** (serves both UI and API), **one free Supabase PostgreSQL database** for permanent user data/cache. This keeps the website on a single hostname with no CORS setup and needs no paid frontend hosting.

**Do not use a local SQLite database on Render free hosting.** Render's free filesystem is ephemeral; data can disappear whenever it sleeps, restarts, or redeploys. Render's own free Postgres databases expire after 30 days, so do **not** use those for important portfolios. Supabase free databases do not have that 30-day expiry but can pause after a week of inactivity and do not include automated backups.

### A. Set up free Supabase database

1. Sign in at **https://supabase.com/** and select **New project** under the Free plan.
2. Store the **database password** somewhere safe. Choose a region near your users/Render service.
3. In **Connect → Connection string**, select the **Session pooler** connection method. This is important: direct connections can be IPv6-only on free projects, while Session Pooler supports IPv4-friendly hosting.
4. Copy the PostgreSQL URI, which typically resembles:

```
postgresql://postgres.PROJECTREF:YOUR_PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres
```

5. This app accepts that URI directly and internally converts `postgresql://` to SQLAlchemy's `postgresql+psycopg://` driver. Add `?sslmode=require` if the URI does not already contain it. Store this as a **secret environment variable**, never in a source file or GitHub repository. If your password contains URL-special characters, percent-encode them in the URI.
6. Supabase automatically creates tables when the FastAPI service first starts (`users`, `portfolios`, `market_cache`). No SQL migration is needed for the initial deployment.

### B. Upload the website source to a *private* GitHub repository

1. Create a new **private repository** at **https://github.com/new**.
2. Push the files in this unzipped project, **excluding `.env`, your actual trades CSV and local `.db` files**. These are covered by `.gitignore`.

```bash
git init
git add .
git commit -m "Build mobile portfolio tracker"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/YOUR_PRIVATE_REPO.git
git push -u origin main
```

> Never publish the sample project's real user data or database credentials. A private repository avoids exposing source and configuration choices, but protection of the deployed app still relies on login and access controls.

### C. Deploy to Render for $0/month within free limits

1. Sign in at **https://dashboard.render.com/**, connect GitHub, and create a **New → Web Service** pointing to that private repository.
2. Use **Python**, choose the **Free** instance plan, and select the region closest to Supabase.
3. Under build command enter:

```bash
pip install -r requirements.txt
```

4. Under start command enter:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT --proxy-headers
```

5. Add these environment variables in Render (**Environment** tab):

| Key | Value |
|---|---|
| `APP_ENV` | `production` |
| `SESSION_SECRET` | A random long value from the Python command above |
| `DATABASE_URL` | The Supabase **Session pooler** URI |
| `PUBLIC_BASE_URL` | `https://YOUR-SERVICE.onrender.com` (actual URL, no trailing slash) |
| `GOOGLE_CLIENT_ID` | Optional until setting up Google login |
| `GOOGLE_CLIENT_SECRET` | Optional until setting up Google login |

6. Click **Deploy**. Render provides HTTPS automatically at `https://YOUR-SERVICE.onrender.com`. Open the site and create a local account.
7. Upload the trades CSV **through Account in the deployed site** (never via GitHub). The uploaded contents persist in Supabase's database, not Render's filesystem.
8. Make a backup periodically using **Account → Download portfolio**.

You can alternatively deploy from the included `render.yaml` blueprint, but the manual steps above make it easier to configure secrets and distinguish the free web instance from the database.

**Limits to understand:** Render Free spins down after 15 minutes idle, and waking up can take about a minute. Free services have monthly allowances and may have intermittent performance/restarts; Render states free instances are intended for hobby/testing, not production. Supabase Free has a 500 MB database cap and can pause after inactivity. They may change their free plans. Check their official pages before deploying.

## 3. Optional: enable Google sign-in

Google OAuth does not work until you provide credentials. Local login works independently.

1. Open **https://console.cloud.google.com/** and create/select a Google Cloud project.
2. In **Google Auth Platform → Branding**, configure your app name, support contact, consent branding, and the allowed audience. For personal use choose **External** with test users if the app is in testing, or publish only after completing any required verification.
3. Under **Google Auth Platform → Clients**, create an **OAuth client ID** of type **Web application**.
4. Add these **Authorized JavaScript origins** (if asked):

```
https://YOUR-SERVICE.onrender.com
```

5. Add this **Authorized redirect URI** exactly:

```
https://YOUR-SERVICE.onrender.com/auth/google/callback
```

6. Copy the Google **Client ID** and **Client secret** into the Render service environment under `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. Ensure `PUBLIC_BASE_URL` matches your public hostname exactly, with **https**. Restart/redeploy if necessary.
7. On the login screen choose **Continue with Google**. Google accounts are keyed internally by the verified Google `sub` (not the email username prefix), so two Google identities cannot collide just because their emails share a prefix. Local users and Google users are distinct accounts. Upload the portfolio to the one you want to use.
8. For local testing with Google, also add `http://127.0.0.1:8000/auth/google/callback` to Google's allowed redirect URIs and use an appropriate local `PUBLIC_BASE_URL` (or omit it and let the app build from the incoming local URL).

## 4. Checks and tests

```bash
pip install pytest
pytest -q
```

The project includes tests of FIFO calculations and CSV compatibility. For serious tax use, compare results with the original Streamlit app, especially historical FX, fees and stock splits.

## Other technical notes

- **Why no Streamlit iframe?** A React/HTML user interface cannot reliably drive Streamlit widgets from outside its embedded iframe; shared Python business logic behind FastAPI is the right separation.
- **Custom tickers:** Use symbols accepted by Yahoo Finance, including exchange suffixes (`VOLV-B.ST`, `INFY.NS`, `BTC-USD`, etc.). Unsupported ticker symbols will produce history warnings.
- **Missing historical quotes:** The `estimated_assets` and `excluded_assets` arrays from `/api/history` explain the coverage. For any serious accounting, reconcile prices before relying on history.
- **Money handling:** Existing Streamlit uses Pandas float arithmetic, which is maintained here for compatibility. For broker-grade accounting, migrate monetary values to integer minor units or Decimal, and add exact transaction IDs/audit trails.
- **No guarantee of no-cost uptime:** The recommended stack is free **within published quotas**, but neither provider offers a free always-on SLA.
- **Security boundaries:** Do not deploy `APP_ENV=development` publicly. Change `SESSION_SECRET`, use HTTPS, set a persistent Postgres database, and do not reuse passwords. Add login rate limiting, user account recovery, MFA and monitoring before opening this as a public multi-user service.

## Troubleshooting

| Problem | What to check |
|---|---|
| Google button missing | Add both `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in Render. |
| `redirect_uri_mismatch` | Compare exact `https://.../auth/google/callback` in Google Console and `PUBLIC_BASE_URL`. |
| Render resets your account | `DATABASE_URL` points to **Supabase**, not ephemeral local SQLite. |
| Postgres connection error | Use Supabase's **Session pooler** URI, correct credentials, and `sslmode=require`. |
| Historical chart slow | First request fetches uncached price/FX series; later requests reuse database cache. |
| Empty/estimated chart | Yahoo may lack specific ticker histories or refuse requests; check ticker codes and upstream service availability. |
| Base currency change fails | The source currency might not have an available historical FX time series. Retain your original base or provide a supported currency. |
| Trade import fails | Upload the source semicolon-delimited 13-column CSV. The original CSV can be imported without internal `trade_id`. |
| Assets feel too low/high | Check Yahoo ticker quote unit (especially GBX vs GBP), stock splits, missing LTP and FX warnings. |

## Provider documentation (checked November 2026)

- Render Free: https://render.com/docs/free
- Supabase Free plan: https://supabase.com/pricing
- Supabase Session Pooler: https://supabase.com/docs/guides/database/connecting-to-postgres
- Google OAuth: https://developers.google.com/identity/protocols/oauth2/web-server

### Appearance and allocation

The Account tab includes a Dark / Light appearance switch stored in the browser (per device). Public Sans is loaded from Google Fonts with system sans-serif fallbacks. Analytics shows a country/region donut based on **trade currency**, not the issuer's domicile or listing exchange: INR → India, USD → USA, SEK → Sweden, EUR → Europe, with mappings for other common currencies and `Other (XXX)` for unrecognized codes. Allocation weights are computed from the **base-currency market values**, while each individual holding's displayed market value in Analytics is in its **trade currency**.
