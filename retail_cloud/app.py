"""Intelligent Cloud Platform for Retail (simple base project).

A central, cloud-deployable platform that connects many retail stores.

- Multi-store: each store has its own API key and sends sales over a REST API
- Central inventory: stock is tracked per store and product in one database
- Analytics dashboard: revenue per store, top products, low stock
- AI 1: Demand forecasting      (linear trend, next 7 days per store/product)
- AI 2: Stock-transfer advice   (move surplus stock from one store to another)
- AI 3: Sales anomaly detection (z-score on daily store revenue)
- Cloud-ready: reads PORT / DATABASE_PATH / ADMIN_KEY from environment,
  has a /health endpoint, and ships with a Dockerfile.

Run:  pip install -r requirements.txt && python app.py
Open: http://127.0.0.1:5000
"""
import os
import random
import secrets
import sqlite3
from datetime import date, timedelta
from statistics import mean, pstdev

from flask import Flask, g, jsonify, render_template_string, request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "platform.db"))
ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin123")
HISTORY_DAYS = 30
FORECAST_DAYS = 7
SAFETY_STOCK = 5

app = Flask(__name__)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS stores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            city TEXT NOT NULL,
            api_key TEXT NOT NULL UNIQUE
        );
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inventory (
            store_id INTEGER NOT NULL REFERENCES stores(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            stock INTEGER NOT NULL,
            PRIMARY KEY (store_id, product_id)
        );
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id INTEGER NOT NULL REFERENCES stores(id),
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity INTEGER NOT NULL,
            sale_date TEXT NOT NULL
        );
        """
    )
    if db.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 0:
        seed(db)
    db.commit()
    db.close()


def seed(db):
    """Demo data: 3 stores, 6 products, 45 days of sales (with one spike)."""
    random.seed(7)
    stores = [("Downtown Store", "Hyderabad"), ("Mall Store", "Bengaluru"), ("Airport Store", "Chennai")]
    products = [
        ("SKU-001", "Milk 1L", "Dairy", 1.2),
        ("SKU-002", "Bread", "Bakery", 1.0),
        ("SKU-003", "Coffee 250g", "Beverages", 6.5),
        ("SKU-004", "Rice 5kg", "Grocery", 9.0),
        ("SKU-005", "Cooking Oil 1L", "Grocery", 5.5),
        ("SKU-006", "Chocolate Bar", "Snacks", 1.8),
    ]
    print("\nDemo store API keys (use in the X-API-Key header):")
    for name, city in stores:
        key = secrets.token_hex(8)
        db.execute("INSERT INTO stores (name, city, api_key) VALUES (?,?,?)", (name, city, key))
        print(f"  {name}: {key}")
    print(f"Admin key: {ADMIN_KEY}\n")
    for sku, name, cat, price in products:
        db.execute("INSERT INTO products (sku, name, category, price) VALUES (?,?,?,?)", (sku, name, cat, price))

    # Different demand profile per store so transfer advice has something to find
    demand = {1: 6, 2: 3, 3: 4}
    stock = {1: (40, 120, 20, 90, 15, 60), 2: (10, 25, 60, 30, 80, 12), 3: (70, 20, 40, 10, 25, 90)}
    for sid in (1, 2, 3):
        for pid in range(1, 7):
            db.execute("INSERT INTO inventory VALUES (?,?,?)", (sid, pid, stock[sid][pid - 1]))
    today = date.today()
    for d in range(45, 0, -1):
        day = today - timedelta(days=d)
        weekend = 1.4 if day.weekday() >= 5 else 1.0
        for sid in (1, 2, 3):
            for pid in range(1, 7):
                base = demand[sid] * (1 + 0.01 * (45 - d)) * weekend  # slight upward trend
                qty = max(0, int(random.gauss(base, 1.5)))
                if sid == 2 and d == 6 and pid in (3, 4):
                    qty += 40  # planted anomaly for the demo
                if qty:
                    db.execute(
                        "INSERT INTO sales (store_id, product_id, quantity, sale_date) VALUES (?,?,?,?)",
                        (sid, pid, qty, day.isoformat()),
                    )


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------
def current_store(db):
    key = request.headers.get("X-API-Key", "")
    return db.execute("SELECT * FROM stores WHERE api_key = ?", (key,)).fetchone() if key else None


def is_admin():
    return secrets.compare_digest(request.headers.get("X-Admin-Key", ""), ADMIN_KEY)


# --------------------------------------------------------------------------
# AI / analytics
# --------------------------------------------------------------------------
def unit_series(db, store_id, product_id, days=HISTORY_DAYS):
    start = date.today() - timedelta(days=days)
    rows = db.execute(
        """SELECT sale_date d, SUM(quantity) q FROM sales
           WHERE store_id=? AND product_id=? AND sale_date>=? GROUP BY sale_date""",
        (store_id, product_id, start.isoformat()),
    ).fetchall()
    by_day = {r["d"]: r["q"] for r in rows}
    return [by_day.get((start + timedelta(days=i)).isoformat(), 0) for i in range(days)]


def linear_forecast(history, horizon=FORECAST_DAYS):
    n = len(history)
    if n == 0:
        return [0.0] * horizon
    xs = range(n)
    mx, my = (n - 1) / 2, mean(history)
    vx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, history)) / vx if vx else 0.0
    intercept = my - slope * mx
    return [max(0.0, intercept + slope * (n + i)) for i in range(horizon)]


def forecasts(db):
    """Forecast for every store/product pair."""
    out = []
    rows = db.execute(
        """SELECT i.store_id, s.name store, i.product_id, p.name product, i.stock
           FROM inventory i JOIN stores s ON s.id=i.store_id JOIN products p ON p.id=i.product_id
           ORDER BY s.id, p.id"""
    ).fetchall()
    for r in rows:
        hist = unit_series(db, r["store_id"], r["product_id"])
        expected = round(sum(linear_forecast(hist)))
        daily = expected / FORECAST_DAYS
        cover = round(r["stock"] / daily, 1) if daily > 0 else None
        shortfall = max(0, expected + SAFETY_STOCK - r["stock"])
        out.append({
            "store_id": r["store_id"], "store": r["store"],
            "product_id": r["product_id"], "product": r["product"],
            "stock": r["stock"], "forecast_7d": expected,
            "days_of_cover": cover, "reorder_qty": shortfall,
            "status": "LOW" if shortfall > 0 else "OK",
        })
    return out


def transfer_suggestions(fc):
    """Move surplus from stores with too much stock to stores that will run short."""
    by_product = {}
    for f in fc:
        by_product.setdefault(f["product_id"], []).append(f)
    moves = []
    for rows in by_product.values():
        surplus = sorted(
            ([r["stock"] - r["forecast_7d"] - SAFETY_STOCK, r] for r in rows if r["stock"] - r["forecast_7d"] - SAFETY_STOCK > 0),
            key=lambda x: -x[0],
        )
        deficit = sorted(
            ([r["forecast_7d"] + SAFETY_STOCK - r["stock"], r] for r in rows if r["reorder_qty"] > 0),
            key=lambda x: -x[0],
        )
        for d in deficit:
            for s in surplus:
                if d[0] <= 0:
                    break
                qty = min(d[0], s[0])
                if qty > 0:
                    moves.append({
                        "product": d[1]["product"], "from_store": s[1]["store"],
                        "to_store": d[1]["store"], "quantity": int(qty),
                    })
                    d[0] -= qty
                    s[0] -= qty
    return moves


def detect_anomalies(db, threshold=2.5):
    """Flag days whose store revenue is > `threshold` std-devs from that store's mean."""
    start = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
    rows = db.execute(
        """SELECT st.id sid, st.name store, sa.sale_date d, SUM(sa.quantity * p.price) rev
           FROM sales sa JOIN stores st ON st.id=sa.store_id JOIN products p ON p.id=sa.product_id
           WHERE sa.sale_date >= ? GROUP BY st.id, sa.sale_date ORDER BY sa.sale_date""",
        (start,),
    ).fetchall()
    series = {}
    for r in rows:
        series.setdefault((r["sid"], r["store"]), []).append((r["d"], r["rev"]))
    found = []
    for (_sid, store), pts in series.items():
        vals = [v for _, v in pts]
        mu, sd = mean(vals), pstdev(vals)
        if sd == 0:
            continue
        for d, v in pts:
            z = (v - mu) / sd
            if abs(z) >= threshold:
                found.append({
                    "store": store, "date": d, "revenue": round(v, 2),
                    "typical": round(mu, 2), "z_score": round(z, 2),
                    "type": "spike" if z > 0 else "drop",
                })
    return sorted(found, key=lambda x: x["date"], reverse=True)


def store_summary(db):
    start = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
    return [dict(r) for r in db.execute(
        """SELECT st.name, st.city,
                  ROUND(COALESCE(SUM(sa.quantity * p.price), 0), 2) revenue,
                  COALESCE(SUM(sa.quantity), 0) units
           FROM stores st
           LEFT JOIN sales sa ON sa.store_id = st.id AND sa.sale_date >= ?
           LEFT JOIN products p ON p.id = sa.product_id
           GROUP BY st.id ORDER BY revenue DESC""", (start,))]


def top_products(db, limit=5):
    start = (date.today() - timedelta(days=HISTORY_DAYS)).isoformat()
    return [dict(r) for r in db.execute(
        """SELECT p.name, p.category, SUM(sa.quantity) units,
                  ROUND(SUM(sa.quantity * p.price), 2) revenue
           FROM sales sa JOIN products p ON p.id = sa.product_id
           WHERE sa.sale_date >= ? GROUP BY p.id ORDER BY revenue DESC LIMIT ?""",
        (start, limit))]


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
PAGE = """
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Intelligent Cloud Platform for Retail</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f3f5f9;color:#1d2433}
 header{background:#0b3d91;color:#fff;padding:16px 24px}
 main{max-width:1150px;margin:20px auto;padding:0 16px}
 .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
 section{background:#fff;border-radius:8px;padding:16px 20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
 h2{margin:0 0 10px;font-size:17px}
 table{width:100%;border-collapse:collapse;font-size:13.5px}
 th,td{text-align:left;padding:7px;border-bottom:1px solid #e6e9ef}
 th{background:#f8f9fb}
 .tag{padding:2px 8px;border-radius:10px;color:#fff;font-size:12px}
 .LOW,.spike{background:#d93025}.OK{background:#188038}.drop{background:#f29900}
 .bar{height:10px;background:#0b3d91;border-radius:3px}
 .muted{color:#6b7280;font-size:13px}
</style></head><body>
<header><h1 style="margin:0;font-size:22px">Intelligent Cloud Platform for Retail</h1>
<div class="muted" style="color:#cfe0ff">Central view of {{ stores|length }} stores &middot; last {{ days }} days</div></header>
<main>
 <div class="grid">
  <section><h2>Revenue by store</h2>
   {% set max_rev = stores[0].revenue or 1 %}
   <table><tr><th>Store</th><th>City</th><th>Units</th><th>Revenue</th></tr>
   {% for s in stores %}<tr><td>{{ s.name }}</td><td>{{ s.city }}</td><td>{{ s.units }}</td>
     <td>{{ "%.2f"|format(s.revenue) }}<div class="bar" style="width:{{ (100*s.revenue/max_rev)|round }}%"></div></td></tr>{% endfor %}
   </table></section>
  <section><h2>Top products</h2>
   <table><tr><th>Product</th><th>Category</th><th>Units</th><th>Revenue</th></tr>
   {% for p in top %}<tr><td>{{ p.name }}</td><td>{{ p.category }}</td><td>{{ p.units }}</td><td>{{ "%.2f"|format(p.revenue) }}</td></tr>{% endfor %}
   </table></section>
 </div>

 <section><h2>AI: stock-transfer suggestions</h2>
  {% if moves %}<table><tr><th>Product</th><th>From</th><th>To</th><th>Quantity</th></tr>
  {% for m in moves %}<tr><td>{{ m.product }}</td><td>{{ m.from_store }}</td><td>{{ m.to_store }}</td><td>{{ m.quantity }}</td></tr>{% endfor %}</table>
  {% else %}<p class="muted">No transfers needed.</p>{% endif %}</section>

 <section><h2>AI: sales anomalies</h2>
  {% if anomalies %}<table><tr><th>Date</th><th>Store</th><th>Revenue</th><th>Typical</th><th>z-score</th><th>Type</th></tr>
  {% for a in anomalies %}<tr><td>{{ a.date }}</td><td>{{ a.store }}</td><td>{{ a.revenue }}</td><td>{{ a.typical }}</td><td>{{ a.z_score }}</td>
   <td><span class="tag {{ a.type }}">{{ a.type }}</span></td></tr>{% endfor %}</table>
  {% else %}<p class="muted">No unusual days detected.</p>{% endif %}</section>

 <section><h2>AI: demand forecast &amp; reorder (next 7 days)</h2>
  <table><tr><th>Store</th><th>Product</th><th>Stock</th><th>Forecast 7d</th><th>Days of cover</th><th>Reorder qty</th><th>Status</th></tr>
  {% for f in fc %}<tr><td>{{ f.store }}</td><td>{{ f.product }}</td><td>{{ f.stock }}</td><td>{{ f.forecast_7d }}</td>
   <td>{{ f.days_of_cover if f.days_of_cover is not none else "-" }}</td><td>{{ f.reorder_qty }}</td>
   <td><span class="tag {{ f.status }}">{{ f.status }}</span></td></tr>{% endfor %}</table></section>
</main></body></html>
"""


@app.route("/")
def dashboard():
    db = get_db()
    fc = forecasts(db)
    return render_template_string(
        PAGE, stores=store_summary(db), top=top_products(db), fc=fc,
        moves=transfer_suggestions(fc), anomalies=detect_anomalies(db), days=HISTORY_DAYS,
    )


# --------------------------------------------------------------------------
# REST API
# --------------------------------------------------------------------------
@app.route("/health")
def health():
    get_db().execute("SELECT 1")
    return jsonify(status="ok")


@app.route("/api/products", methods=["GET", "POST"])
def api_products():
    db = get_db()
    if request.method == "GET":
        return jsonify([dict(r) for r in db.execute("SELECT * FROM products")])
    if not is_admin():
        return jsonify(error="admin key required (X-Admin-Key)"), 401
    data = request.get_json(silent=True) or {}
    try:
        cur = db.execute(
            "INSERT INTO products (sku, name, category, price) VALUES (?,?,?,?)",
            (data["sku"], data["name"], data["category"], float(data["price"])),
        )
        for s in db.execute("SELECT id FROM stores"):
            db.execute("INSERT INTO inventory VALUES (?,?,0)", (s["id"], cur.lastrowid))
        db.commit()
    except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
        return jsonify(error=f"invalid product: {exc}"), 400
    return jsonify(id=cur.lastrowid), 201


@app.route("/api/stores", methods=["POST"])
def api_add_store():
    if not is_admin():
        return jsonify(error="admin key required (X-Admin-Key)"), 401
    db = get_db()
    data = request.get_json(silent=True) or {}
    key = secrets.token_hex(8)
    try:
        cur = db.execute("INSERT INTO stores (name, city, api_key) VALUES (?,?,?)",
                         (data["name"], data["city"], key))
        for p in db.execute("SELECT id FROM products"):
            db.execute("INSERT INTO inventory VALUES (?,?,0)", (cur.lastrowid, p["id"]))
        db.commit()
    except (KeyError, sqlite3.IntegrityError) as exc:
        return jsonify(error=f"invalid store: {exc}"), 400
    return jsonify(id=cur.lastrowid, api_key=key), 201


@app.route("/api/inventory")
def api_inventory():
    store = current_store(get_db())
    if store is None:
        return jsonify(error="valid X-API-Key required"), 401
    rows = get_db().execute(
        """SELECT p.sku, p.name, i.stock FROM inventory i JOIN products p ON p.id=i.product_id
           WHERE i.store_id=?""", (store["id"],))
    return jsonify([dict(r) for r in rows])


@app.route("/api/stock", methods=["POST"])
def api_add_stock():
    """A store records received stock: {"sku": "SKU-001", "quantity": 50}"""
    db = get_db()
    store = current_store(db)
    if store is None:
        return jsonify(error="valid X-API-Key required"), 401
    data = request.get_json(silent=True) or {}
    p = db.execute("SELECT id FROM products WHERE sku=?", (data.get("sku"),)).fetchone()
    try:
        qty = int(data.get("quantity", 0))
    except (TypeError, ValueError):
        qty = 0
    if p is None or qty < 1:
        return jsonify(error="need a valid sku and quantity >= 1"), 400
    db.execute("UPDATE inventory SET stock = stock + ? WHERE store_id=? AND product_id=?",
               (qty, store["id"], p["id"]))
    db.commit()
    return jsonify(status="stock added")


@app.route("/api/sales", methods=["POST"])
def api_sales():
    """A store reports a sale: {"sku": "SKU-001", "quantity": 2}"""
    db = get_db()
    store = current_store(db)
    if store is None:
        return jsonify(error="valid X-API-Key required"), 401
    data = request.get_json(silent=True) or {}
    p = db.execute("SELECT id FROM products WHERE sku=?", (data.get("sku"),)).fetchone()
    try:
        qty = int(data.get("quantity", 0))
    except (TypeError, ValueError):
        qty = 0
    if p is None or qty < 1:
        return jsonify(error="need a valid sku and quantity >= 1"), 400
    inv = db.execute("SELECT stock FROM inventory WHERE store_id=? AND product_id=?",
                     (store["id"], p["id"])).fetchone()
    if inv is None or inv["stock"] < qty:
        return jsonify(error="insufficient stock", available=inv["stock"] if inv else 0), 409
    db.execute("INSERT INTO sales (store_id, product_id, quantity, sale_date) VALUES (?,?,?,?)",
               (store["id"], p["id"], qty, date.today().isoformat()))
    db.execute("UPDATE inventory SET stock = stock - ? WHERE store_id=? AND product_id=?",
               (qty, store["id"], p["id"]))
    db.commit()
    return jsonify(status="sale recorded", remaining=inv["stock"] - qty), 201


@app.route("/api/insights")
def api_insights():
    db = get_db()
    fc = forecasts(db)
    return jsonify(
        stores=store_summary(db), top_products=top_products(db), forecast=fc,
        transfer_suggestions=transfer_suggestions(fc), anomalies=detect_anomalies(db),
    )


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
