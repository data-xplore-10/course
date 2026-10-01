# Customer Markup Rates — example app

A small, dependency-free app for configuring the markup each customer pays.

- Every customer has a **default markup %**.
- Any product category (Hardware, Software, Services, Shipping) can be given a
  **per-customer override**; clearing it falls back to the default.
- A **price calculator** resolves the effective rate for a customer + category
  and applies it to a unit cost: `price = cost × (1 + markup / 100)`, rounded
  half-up to cents.

Built with only the Python standard library (`http.server` + `sqlite3`) and a
single vanilla-JS page, so it runs anywhere Python 3.10+ is installed.

## Run

```bash
cd markup_rates_app
python app.py               # http://localhost:8000
```

On first start it creates `markup_rates.db` and seeds three sample customers.
Set `PORT` or `MARKUP_DB` to change the port or database file.

## Test

```bash
python -m unittest test_app.py
```

## API

| Method | Path | Body | Purpose |
| --- | --- | --- | --- |
| GET | `/api/categories` | | List product categories |
| GET | `/api/customers` | | List customers with their overrides |
| POST | `/api/customers` | `{"name", "default_markup"}` | Create a customer |
| PUT | `/api/customers/{id}` | `{"name"?, "default_markup"?}` | Update a customer |
| DELETE | `/api/customers/{id}` | | Delete a customer (and its overrides) |
| PUT | `/api/customers/{id}/rates/{category}` | `{"markup"}` | Set a category override |
| DELETE | `/api/customers/{id}/rates/{category}` | | Clear a category override |
| GET | `/api/quote?customer_id=&category=&cost=` | | Calculate a price |

Markups must be between 0 and 500 and are stored as exact decimals (no float
rounding). Invalid input returns `400` with `{"error": "..."}`.

## Files

- `app.py` — data layer (`Store`), validation, and HTTP routes
- `static/index.html` — the UI
- `test_app.py` — unit tests for pricing rules plus API round-trip tests
