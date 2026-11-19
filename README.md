# Track portfolio workspace

Track is a FastAPI application with a responsive HTML, CSS and JavaScript frontend. It runs with Python and serves its interface, fonts and charts from the same app.

## Run locally

From the directory containing `app.py`, create a virtual environment and install the dependencies.

```sh
python -m venv .venv
# macOS or Linux
source .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m uvicorn app:app --reload
```

Open [Track locally](http://127.0.0.1:8000). Create an account, then use **New trade** or **Account → Import portfolio**. Import accepts the original semicolon-delimited CSV. Local development uses `portfolio_local.db` in the project directory. Existing databases and exported portfolios remain compatible.

Production requires a persistent database and a strong `SESSION_SECRET`. To generate a secret, run `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Google sign-in is optional. The deployment instructions below list its settings.

## Workspace

- **Overview** shows the current value, FIFO invested capital, unrealized P/L, allocation by asset class and open holdings. Select a holding for last price, average price and quantity.
- **Value history** supports 1M, 3M, 6M, 1Y, 3Y and MAX. Hover, tap or focus the chart and use the arrow keys to inspect a point. It measures portfolio market value, including the effects of purchases and sales. Valuation notes identify estimates and exclusions.
- **Analytics** provides an interactive asset treemap and detailed positions. Allocation uses base-currency market values. Individual position details show the trade currency.
- **New trade** supports purchases and sales, brokerage fees, STT and either automatic or manual trade-date FX. The preview shows the trade value before fees and tax.
- **Trade history** supports searching, paging, editing and deleting transactions.
- **Tax report** shows realized FIFO results for preset or custom dates and exports CSV. Trade-currency results are reported separately.
- **Account** includes base currency, light and dark themes, balance masking, CSV import and export, and sign-out. Import replaces the active account's trades.

## Refresh and history

Price refresh reuses the active symbols from the dashboard. On a direct refresh, the existing FIFO matcher identifies open positions. Successful quotes are applied together, while unavailable quotes keep their stored prices. An unchanged quote batch skips the portfolio write. The interface receives the refreshed dashboard in the same response. A concurrent portfolio edit takes precedence over an in-flight refresh.

History accumulates quantity on trade dates, carries it onto the requested daily calendar, and aligns quote and FX series by column. Overlapping requests can share provider connections and identical in-flight downloads. Concurrent requests for the same account, chart inputs and range share one calculation. A five-minute chart cache is keyed by valuation inputs, base currency, day and the relevant official NSE close. A latest-price-only update can reuse the daily history. The first request for uncached market data still depends on provider latency.

### Accounting and prices

The accounting implementation is in `portfolio.py`. Daily valuation carries forward available market closes and uses transaction prices when closes are unavailable. Historical FX uses the existing forward/backfill and recorded-transaction fallback rules. The history response includes estimated and excluded assets. Daily values keep the original cent rounding, output dates and sampling.

FIFO matches buys before sells at the same timestamp and retains the CSV ordering rule within each side. Economic base cost includes brokerage and STT. Base-currency INR realized P/L excludes STT from cost and proceeds. Trade-currency realized P/L also excludes STT. Unmatched sales are flagged and the unmatched quantity is excluded from FIFO realized P/L.

Current valuation uses stored trade-currency prices and spot conversion. Changing the base currency recomputes transaction-date FX. GBX conversion retains the original pence scaling. After the Indian trading session, covered .NS tickers use the official NSE closing file when available. Fallbacks follow the existing `PRICE_SOURCE_PRIORITY` setting. Other tickers use Yahoo intraday quotes. The chart uses daily closes, Frankfurter FX and the latest applicable cached official NSE close.

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
2. Push the files in this unzipped project, **excluding `.env`, your actual trades CSV and local `.db` files**. Keep these files outside source control.

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
8. Make a backup periodically using **Account → Export portfolio**.

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

After installing the requirements, run the regression suite from the project root.

```sh
python -m unittest discover -s tests -v
```

The tests use controlled market responses to cover short-window balances, missing prices and FX, empty and one-day histories, quote eligibility, cache invalidation, parallel refresh and concurrent chart requests.

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
| Historical chart slow | The first request may wait for uncached prices and FX. Later requests reuse market data and computed ranges. |
| Empty/estimated chart | Yahoo may lack specific ticker histories or refuse requests; check ticker codes and upstream service availability. |
| Base currency change fails | The source currency might not have an available historical FX time series. Retain your original base or provide a supported currency. |
| Trade import fails | Upload the source semicolon-delimited 13-column CSV. The original CSV can be imported without internal `trade_id`. |
| Assets feel too low/high | Check Yahoo ticker quote unit (especially GBX vs GBP), stock splits, missing LTP and FX warnings. |

## Provider documentation (checked November 2026)

- Render Free: https://render.com/docs/free
- Supabase Free plan: https://supabase.com/pricing
- Supabase Session Pooler: https://supabase.com/docs/guides/database/connecting-to-postgres
- Google OAuth: https://developers.google.com/identity/protocols/oauth2/web-server
