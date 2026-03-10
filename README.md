# Meta Ads Performance Dashboard — Flask App

A real-time, interactive Meta Ads dashboard for **GT Bharathi Projects** and **Bharathi Meraki**, built with Flask and the Meta Ads API.

---

## Features

- **Live data** pulled from Meta Ads API on every page load
- **Date range picker** — choose any custom range, or use quick presets (7 days, 14 days, 30 days, This Week, This Month)
- **Clickable campaigns** — drill into any campaign to see Ad Set and Ad level breakdowns
- **3-level hierarchy**: Dashboard → Campaign → Ad Sets + Ads
- **Interactive charts** powered by Chart.js
- **Scatter plot** of Spend vs Leads per ad (on the campaign detail page)
- **Color-coded CPL** — green < ₹300, amber < ₹600, red ≥ ₹600
- **Loading overlay** for all navigations and API fetches
- Dark luxury UI matching your original dashboard design

---

## Project Structure

```
meta_ads_dashboard/
├── app.py                  # Flask routes + Meta API logic
├── requirements.txt
├── .env.example            # Template for your credentials
├── .env                    # Your credentials (do not commit!)
└── templates/
    ├── dashboard.html      # Main dashboard view
    └── campaign.html       # Campaign drill-down view
```

---

## Setup

### 1. Install dependencies

```bash
cd meta_ads_dashboard
pip install -r requirements.txt
```

### 2. Configure credentials

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:
```
META_ACCESS_TOKEN=EAAxxxxxxxxxxxxx...   # Your long-lived User or System User token
META_APP_ID=123456789012345
META_APP_SECRET=abcdef1234567890abcdef
```

#### How to get a long-lived access token

1. Go to [Meta Business Suite](https://business.facebook.com/) → Settings → System Users
2. Create a System User and assign it to your ad accounts
3. Generate an access token with the `ads_read` permission
4. Use the [Graph API Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/) to verify it

### 3. Load environment variables

Add this to the top of `app.py` (already included):

```python
from dotenv import load_dotenv
load_dotenv()
```

Or export manually before running:

```bash
export META_ACCESS_TOKEN="EAAxxxxx..."
export META_APP_ID="123456789"
export META_APP_SECRET="abcdef..."
```

### 4. Run the app

```bash
python app.py
```

Then open [http://localhost:5050](http://localhost:5050)

---

## API Routes

| Route | Description |
|---|---|
| `GET /` | Main dashboard. Accepts `?date_start=YYYY-MM-DD&date_end=YYYY-MM-DD` |
| `GET /campaign/<id>` | Campaign drill-down. Accepts same date params + `?name=...` |
| `GET /api/summary` | JSON: campaign-level data for a date range |
| `GET /api/campaign/<id>` | JSON: ad set + ad level data for a campaign |

---

## Ad Account IDs

| Account | ID |
|---|---|
| GT Bharathi Projects | `act_522598556166911` |
| Bharathi Meraki | `act_991827431878117` |

These are hardcoded in `app.py` under `ACCOUNTS`. Update if they change.

---

## Attribution

All lead data uses:
- `action_type = "lead"`
- Attribution window: `7d_click + 1d_view`
- Meta Ads API v21

---

## Deployment

For production, use **Gunicorn** behind Nginx:

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5050 app:app
```

Or deploy to **Railway**, **Render**, or **Heroku** — set your three env vars in the platform's dashboard.

---

## Troubleshooting

| Issue | Fix |
|---|---|
| `OAuthException` | Token expired — regenerate a long-lived token |
| Empty dashboard | Check account IDs and token permissions (`ads_read`) |
| No leads showing | Verify attribution settings match your Meta setup |
| Slow page loads | Meta API can take 2–5s per account — expected behaviour |
