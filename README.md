# Store Admin Panel

Independent admin panel and backend management system for Somphea Reak Studio.

## Included Components
- **`admin_routes.py`**: Complete Flask Blueprint (`admin_bp`) managing authentication, orders, inventory stock matrix, categories, minifig lucky draw / spin management, and promo codes.
- **`models.py`**: Shared SQLAlchemy ORM models (`Product`, `Category`, `Order`, `PromoCode`, `MinifigurePool`, `DrawHistory`, `RedeemCode`, `PlayerSession`).
- **`templates/`**: Full suite of enterprise admin interface templates (`admin/base.html`, `admin/dashboard.html`, `admin/inventory.html`, `admin/orders.html`, `admin/redeem.html`, `admin/spin.html`, `admin_login.html`).

## Integration with Storefront (`app.py`)
In the main storefront application, register the admin blueprint:
```python
import importlib
store_admin_module = importlib.import_module('store-admin')
app.register_blueprint(store_admin_module.admin_bp)
```

## Security
- Credentials and database URLs are loaded from environment variables (`ADMIN_USERNAME`, `ADMIN_PASS`, `DATABASE_URL`, `SECRET_KEY`).
- Constant-time password comparison via `hmac.compare_digest` to prevent timing attacks.
- Strict `@login_required` enforcement on all dashboard and mutation routes.
