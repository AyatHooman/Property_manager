# 🏠 Property Manager

Australian property search tool powered by the **Domain API**.

## Features
- 🔍 Search **for-sale and rental listings** by suburb
- 📊 View **suburb performance** (median prices, days on market)
- 🏷️ Browse **recent sales results**
- ⭐ **Save** and manage favourite listings locally
- 🕑 View **search history**
- 💾 Local **SQLite cache** to minimise API calls

---

## Setup

### 1. Get Domain API credentials
1. Register at [developer.domain.com.au](https://developer.domain.com.au)
2. Create a new app → note your **Client ID** and **Client Secret**

### 2. Configure credentials
```bash
copy .env.example .env
```
Edit `.env` and fill in your credentials:
```
DOMAIN_CLIENT_ID=your_client_id_here
DOMAIN_CLIENT_SECRET=your_client_secret_here
```

### 3. Install dependencies
```bash
pip install -r requirements.txt
```

---

## Usage

```bash
# Search for sale listings in Surry Hills, NSW
python -m src.cli search "Surry Hills" NSW

# Search for rentals with filters
python -m src.cli search "Bondi Beach" NSW --type Rent --min-beds 2 --max-price 3000

# Suburb performance stats
python -m src.cli suburb "Newtown" NSW 2042

# Recent sales results
python -m src.cli sales "Manly" NSW 2095

# Autocomplete a suburb name
python -m src.cli suggest "surr"

# Save a listing by ID
python -m src.cli save 12345678

# View saved listings
python -m src.cli saved

# Remove a saved listing
python -m src.cli unsave 12345678

# View recent search history
python -m src.cli history
```

---

## Project Structure
```
Property_manager/
├── src/
│   ├── auth.py          # OAuth2 token management
│   ├── api_client.py    # Domain API wrapper
│   ├── database.py      # SQLite cache + saved listings
│   ├── cli.py           # CLI entry point
│   └── models.py        # Data models
├── data/                # Auto-created, holds property_cache.db
├── .env                 # Your credentials (never commit!)
├── .env.example         # Credentials template
└── requirements.txt
```

---

## API Rate Limits
The free Domain API tier allows **500 requests/day**. The app caches responses for 1 hour automatically to stay within limits.
