<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img src="assets/logo-light.svg" alt="purchasing-bot" width="440">
  </picture>
</p>

# Bulk Purchasing Bot

**Safe, reliable bulk purchasing** against [demoblaze.com](https://www.demoblaze.com), a public
test store made for automation practice. The aim is correctness, not speed. Every order line goes through a
recorded state machine. The cart is built idempotently and reconciled before paying. "Purchase" is never
clicked twice blindly.

> Only fake card data is used. The bot refuses any card number that is not a well-known test number
> such as `4111 1111 1111 1111`.

## Setup (Windows)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # or call .\.venv\Scripts\python.exe directly
pip install -r requirements.txt
playwright install chromium
python -m bot signup                   # creates a fresh test user and writes it to .env
```

The bot needs Python 3.11 or newer (tested on 3.12). `.env.example` lists the settings: `DEMOBLAZE_USER`,
`DEMOBLAZE_PASS` and the fake checkout fields (`CARD_NUMBER`, `CARD_NAME`, ...).

## Commands

```powershell
python -m bot signup                              # new test user via /signup -> .env
python -m bot run orders.csv                      # DRY RUN (default): build + reconcile, stop before Purchase
python -m bot run orders.csv --live --batch-size 4
python -m bot run --resume <run_id> --live        # continue from the ledger
python -m bot runs                                # list runs
python -m bot report <run_id>                     # regenerate the reports
```

| Flag | Meaning |
|---|---|
| `--live` | Actually click Purchase. **Without it every run is a dry run.** |
| `--mode api\|browser` | `api` (default): catalog, login, add-to-cart and cart checks go through the API, and the browser is used only for checkout. `browser`: the whole flow through the UI (the fallback). |
| `--batch-size N` | N order lines per checkout. Each batch gets its own cart and confirmation. `0` = one checkout. |
| `--simulate ...` | Simulation switches (repeatable or comma separated): `price_drift=<prod_id>:<pct>`, `stock_out=<prod_id>`, `checkout_timeout=<batch_no>`, `interrupt=<batch_no>` |
| `--stock FILE` / `--no-stock` | Simulated inventory (default: `stock.yaml` if present) |
| `--allow-partial` | Short stock -> buy what is left (`PARTIAL`) instead of `SKIP_STOCK` |
| `--strict` | Unexpected items in the cart abort the run instead of being removed |
| `--headed`, `--slowmo MS` | Show the browser and slow it down so the client can watch |
| `--rps 2` | API throttle (default about 2 requests/s, because it is a shared public site) |
| `--quiet-api` | Hide the per-request `[api]` log lines |

Exit codes: `0` completed or dry run, `2` aborted or completed with issues, `130` interrupted.

## orders.csv

```csv
product,qty,max_unit_price
1,2,400                 <- Demoblaze id
Nexus 6,1,700           <- or title (case-insensitive)
...
Google Pixel 9,1,500    <- unknown product -> REJECTED
HTC One M9,0,750        <- qty <= 0        -> REJECTED
```

In the sample file, `Iphone 6 32gb` costs 790 with a max of 700, so the price guard skips it even without
simulation. `Sony xperia z5` asks for 3 while `stock.yaml` has 2, so it is `SKIP_STOCK`, or `PARTIAL` with
`--allow-partial`.

## What it proves

1. **Bulk input.** `orders.csv` is validated against the live `/entries` catalog (paginated). Bad rows are
   recorded as `SKIPPED / REJECTED` with the reason.
2. **API first.** `/login`, `/entries`, `/view`, `/addtocart`, `/viewcart` and `/deleteitem` run over httpx.
   The browser reuses the API session (it receives the same `tokenp_` cookie) and does only the checkout,
   because Demoblaze has no order API. `--mode browser` does everything through the UI: login modal,
   product pages, the "Product added" alert, cart rows. It captures the uuid the page generates from the
   `/addtocart` request.
3. **Price guard.** The live price from `/view` is compared with `max_unit_price`.
   `--simulate price_drift=1:20` raises the observed price to show the guard firing. A drift small enough
   to pass the guard is still caught by step 6: the computed total no longer matches the cart page, so the
   run aborts before paying.
4. **Stock guard (SIMULATED).** Demoblaze has no inventory, so `stock.yaml` and
   `--simulate stock_out=<id>` are a simulation layer that exists only in this bot. Lines become `BUY`,
   `PARTIAL` (with `--allow-partial`) or `SKIP_STOCK`.
5. **Idempotent cart building.** The planner generates one uuid per unit (qty 5 means 5 `/addtocart`
   calls) and stores them in the ledger before anything is added. Each build reads `/viewcart` first and
   adds only the missing uuids. Demoblaze also upserts by uuid, so a retried request cannot duplicate a
   line.
6. **Pre-checkout reconciliation.** `/viewcart` must contain every expected uuid with the right product.
   Unexpected lines are removed with `/deleteitem`, or abort the run with `--strict`. The rows on
   `cart.html` and `#totalp` must equal the computed total. The computed total uses integer prices, like
   `cart.js`. Any mismatch marks the batch `FAILED`, takes a screenshot and aborts the run.
7. **Checkout safety.** The ledger records `CHECKOUT_SENT` *before* the Purchase click. If anything fails
   after the click (timeout, browser crash, process killed), the bot does **not** click again. It polls
   `/viewcart` for a few seconds first, because the site clears the cart asynchronously:
   - none of the batch's items left -> the purchase went through -> `CONFIRMED_UNVERIFIED`
   - all items still there -> nothing was bought -> rebuild, reconcile and retry **once**
   - partially cleared or unreadable -> `UNCERTAIN`, and the run stops for a human

   `--simulate checkout_timeout=1` shows this live.
8. **Batching.** `--batch-size N` creates one cart and one checkout per batch, each with its own parsed
   confirmation.
9. **Resume.** `--resume <run_id>` continues from the ledger. The plan is not recomputed, and batches in
   `CONFIRMED`, `CONFIRMED_UNVERIFIED` or `UNCERTAIN` are never touched again. A batch left in
   `CHECKOUT_SENT` by a crash is resolved through `/viewcart` first.
10. **Dry run by default.** The bot builds and reconciles each batch, takes a screenshot of the cart, then
    empties the cart again. `--live` is required to click Purchase. A dry run can later be resumed with
    `--live`.

## Ledger (`ledger.db`, SQLite)

- `runs`: mode, live flag, options, status (`PLANNED`, `RUNNING`, `DRY_RUN_COMPLETE`, `COMPLETED`,
  `COMPLETED_WITH_ISSUES`, `ABORTED`, `INTERRUPTED`)
- `batches`: state, purchase attempts, expected total, `#totalp`, and the parsed order id, amount, card
  last 4, name, date, plus a screenshot and the error text
- `lines`: input, matched product, requested and planned qty, live price, seen price, decision, state,
  error
- `cart_units`: one row per unit (`uuid`, `prod_id`, `PLANNED`/`ADDED`/`REMOVED`/`PURCHASED`)
- `events`: every state transition with a timestamp, used for the state history in the reports

Line and batch state machine (illegal transitions raise an error):

```
PENDING -> VALIDATED -> IN_CART -> RECONCILED -> CHECKOUT_SENT -> CONFIRMED | CONFIRMED_UNVERIFIED
   \-> SKIPPED (rejected / price / stock)                       \-> FAILED | UNCERTAIN
```

## Output

- A Rich live table in the console (lines and batches), with a log stream above it.
- `runs/<run_id>/report.csv` and `report.html`: ordered units, skipped, failed, spend, per-batch order ids
  and the per-line state history.
- `runs/<run_id>/screens/`: one screenshot per confirmation dialog and per failure, plus the reconciled
  cart in dry runs.
- `runs/LAST_RUN` holds the most recent run id.

The SweetAlert text is parsed into order id, amount, card (stored masked to its last 4 digits), name and
date. `cart.js` builds the date with JavaScript's 0-based `getMonth()`, so the dialog's `12/9/2026` is
stored as `2026-10-12` (the raw text is kept too). The confirmation screenshot shows the full test card
number, because that is what the site displays.

## Tests

```powershell
python -m pytest
```

Demoblaze is replaced by an in-memory fake behind `httpx.MockTransport`, plus a fake checkout page. The
tests cover:

- resume never re-adds units that are already in the cart (after a network failure and after an interrupt)
- a checkout timeout followed by an empty cart -> `CONFIRMED_UNVERIFIED` and no second Purchase click
- a timeout with the cart untouched -> exactly one retry; two failed attempts -> no third click
- a crash after the click is resolved on resume without clicking again
- reconciliation mismatches abort before paying (total mismatch, a missing unit, strays with `--strict`,
  price drift that passes the guard)
- the guards, SweetAlert parsing, card masking, the test-card guard, the throttle and the ledger's
  transition checks

## Demoblaze quirks handled

- There is no quantity field: one `/addtocart` call per unit, each with its own uuid.
- There is no server-side order or stock. "Place order" only checks Name and Card, fires `/deletecart`
  and shows a SweetAlert with a random id. So "the cart was emptied" is the only server-side evidence of a
  purchase.
- `purchaseOrder()` clears the cart of the username that `/check` sets on page load. The bot waits until
  `#nameofuser` is visible before clicking, otherwise the wrong (empty) cart would be cleared.
- The dialog's OK button navigates away and can abort the site's own `/deletecart` request. The bot waits
  for that response before clicking OK, and checks the cart afterwards anyway (clearing it itself if the
  site did not).
- `cart.html` renders rows asynchronously (one `/view` per item). The bot waits for the row count reported
  by `/viewcart` before reading `#totalp`.
