# UNIQLO KR Product Scraper

UNIQLO Korea exposes product lists through the public commerce API, for example:

```text
https://www.uniqlo.com/kr/api/commerce/v5/ko/products?path=57892%2C57959%2C%2C&limit=100
```

This tool collects category paths from the taxonomy API, fetches product lists
with pagination, and writes JSON and optional CSV output. The older HTML crawl
path is still available with `--mode html`.

The products API currently accepts `limit` values up to `100`. When a category
has more products than the configured limit, the scraper automatically advances
`offset` until the API-reported `pagination.total` is exhausted.

## Usage

```bash
python3 uniqlo_kr_scraper.py --out data/uniqlo_kr_products.json --csv data/uniqlo_kr_products.csv --verbose
```

Useful options:

```bash
python3 uniqlo_kr_scraper.py --api-path '57892,57959,,' --api-limit 100 --out data/women_tops.json
python3 uniqlo_kr_scraper.py --taxonomy-depth category --delay 0.3
python3 uniqlo_kr_scraper.py --details --max-details 50
python3 uniqlo_kr_scraper.py --insecure --out data/uniqlo_kr_products.json
python3 uniqlo_kr_scraper.py --mode html --max-pages 1000 --delay 0.3
```

Export the full category list separately:

```bash
python3 uniqlo_kr_scraper.py --mode categories --out data/uniqlo_kr_categories.json --csv data/uniqlo_kr_categories.csv --insecure
```

Build a local web page for discounted products:

```bash
python3 uniqlo_kr_scraper.py --mode discounts --insecure
```

This writes:

- `data/uniqlo_kr_discount_items.json`
- `data/uniqlo_kr_discount_items.csv`
- `data/uniqlo_kr_discount_items.html`

## Static Site

The `docs/` directory is a deployable static site root. It contains:

- `docs/index.html`
- `docs/data/uniqlo_kr_discount_items.json`
- `docs/data/uniqlo_kr_discount_items.csv`
- `docs/data/uniqlo_kr_all_items.json`
- `docs/data/uniqlo_kr_categories.json`

For GitHub Pages, set the Pages source to `docs/`.

## Scheduled Updates

The GitHub Actions workflow at `.github/workflows/update-uniqlo-data.yml`
updates the data once a day at 03:15 KST and can also be run manually from the
Actions tab with `workflow_dispatch`.

The workflow refreshes all products, categories, and discounted products, copies
the static-site data into `docs/data/`, and commits only when the generated data
changes. GitHub Pages will then serve the updated `docs/` files after the commit.

Use `--insecure` only when the local Python certificate store cannot verify the
site certificate.

## Output

The JSON file includes crawl metadata and a `products` array. Each product may
include:

- `product_id`
- `product_number`
- `name`
- `gender_category`
- `price_krw`
- `colors`
- `sizes`
- `rating_average`
- `rating_count`
- `image_url`
- `detail_url`

## Notes

This scraper uses only public website responses. UNIQLO can change page
structure, throttling, or rendered state keys at any time, so schedule runs with
a polite delay and verify output counts after site updates.
