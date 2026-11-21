import os
import json
import random
import string
import uuid
import hmac
import time
from datetime import datetime
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, session, flash
from models import (
    db, Product, Category, Order, PromoCode, PlayerSession,
    RedeemCode, MinifigurePool, DrawHistory, CustomerUser,
    CreditTransaction, SystemSetting, get_reward_config,
    save_reward_config, _sync_product_to_pool, _sync_pool_to_product
)
import cloudinary.uploader

# --- LOAD ENVIRONMENT VARIABLES (.env) ---
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def _load_env_fallback(filepath='.env'):
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    os.environ.setdefault(key, val)

_load_env_fallback()

try:
    from uploader import optimize_and_upload
except ImportError:
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from uploader import optimize_and_upload

admin_bp = Blueprint('admin', __name__, template_folder='templates')

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'Thesong_Admin@2022?!$')

def _safe_int(val, default=0):
    try:
        if val is None or str(val).strip() == '':
            return default
        return int(float(str(val).strip()))
    except (ValueError, TypeError):
        return default

def _safe_float(val, default=0.0):
    try:
        if val is None or str(val).strip() == '':
            return default
        return float(str(val).strip())
    except (ValueError, TypeError):
        return default

def generate_unique_product_code(prefix="SKU"):
    """
    Auto-generates a clean, unique product code (e.g. SKU-1001, SKU-1002...).
    Guarantees no two products can ever be generated or assigned the same code.
    """
    existing_codes = set()
    rows = db.session.query(Product.product_code).filter(Product.product_code.isnot(None)).all()
    for (r,) in rows:
        if r:
            existing_codes.add(r.strip().upper())

    max_num = 1000
    for code in existing_codes:
        if code.startswith(f"{prefix}-"):
            part = code[len(prefix)+1:]
            if part.isdigit():
                max_num = max(max_num, int(part))
        elif code.startswith(prefix):
            part = code[len(prefix):]
            if part.isdigit():
                max_num = max(max_num, int(part))

    candidate_num = max_num + 1
    while True:
        candidate = f"{prefix}-{candidate_num:04d}"
        if candidate not in existing_codes:
            return candidate
        candidate_num += 1

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('admin'):
            flash('Please log in to access the admin panel.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def _deduct_order_stock(order):
    """Safely and atomically deduct item stock and promo usage for confirmed/processing orders."""
    if not order or order.stock_deducted:
        return
    try:
        items = json.loads(order.items_json) if order.items_json else []
        for item in items:
            identifier = str(item.get('variantId') or item.get('cartId') or item.get('id') or '')
            parts = identifier.split('-')
            qty = max(1, int(item.get('qty', 1)))
            
            if len(parts) >= 2 and parts[0].isdigit():
                p_id = int(parts[0])
                v_target = int(parts[1]) if parts[1].isdigit() else -1
                product = Product.query.get(p_id)
                if product:
                    if product.variants and v_target != -1:
                        try:
                            variants = json.loads(product.variants)
                            matched_v = None
                            # Match by variant index or variant id
                            if 0 <= v_target < len(variants) and variants[v_target].get('id') == v_target:
                                matched_v = variants[v_target]
                            else:
                                for v in variants:
                                    if v.get('id') == v_target:
                                        matched_v = v
                                        break
                                if not matched_v and 0 <= v_target < len(variants):
                                    matched_v = variants[v_target]

                            if matched_v:
                                matched_v['stock'] = max(0, int(matched_v.get('stock', 0)) - qty)
                                product.variants = json.dumps(variants)
                                product.stock = sum(int(v.get('stock', 0)) for v in variants)
                                _sync_product_to_pool(p_id, matched_v.get('id', v_target), matched_v['stock'])
                            else:
                                product.stock = max(0, product.stock - qty)
                                _sync_product_to_pool(p_id, -1, product.stock)
                        except Exception as e:
                            product.stock = max(0, product.stock - qty)
                            _sync_product_to_pool(p_id, -1, product.stock)
                    else: 
                        product.stock = max(0, product.stock - qty)
                        _sync_product_to_pool(p_id, -1, product.stock)
            elif identifier.isdigit():
                p_id = int(identifier)
                product = Product.query.get(p_id)
                if product:
                    product.stock = max(0, product.stock - qty)
                    if product.variants:
                        try:
                            vars_list = json.loads(product.variants)
                            if vars_list:
                                vars_list[0]['stock'] = max(0, int(vars_list[0].get('stock', 0)) - qty)
                                product.variants = json.dumps(vars_list)
                        except Exception:
                            pass
                    _sync_product_to_pool(p_id, -1, product.stock)
    except Exception as e:
        print(f"Stock deduction error for order {order.id}: {e}")

    order.stock_deducted = True
    try:
        db.session.commit()
    except Exception:
        pass

admin_failed_logins = {}

# --- ADMIN AUTH & NAVIGATION ROUTES ---
@admin_bp.route('/admin/login', methods=['GET', 'POST'])
def login():
    forwarded = request.headers.get('X-Forwarded-For')
    client_ip = forwarded.split(',')[0].strip() if forwarded else (request.remote_addr or '127.0.0.1')
    now = time.time()

    attempts = [ts for ts in admin_failed_logins.get(client_ip, []) if now - ts < 300]
    if len(attempts) >= 5:
        retry_after = max(1, int(300 - (now - attempts[0])))
        flash(f'Too many failed admin login attempts. Please wait {retry_after} seconds.', 'error')
        return render_template('admin_login.html'), 429

    if request.method == 'POST':
        user_input = request.form.get('username', '')
        pass_input = request.form.get('password', '')
        if hmac.compare_digest(user_input, ADMIN_USERNAME) and hmac.compare_digest(pass_input, ADMIN_PASS):
            admin_failed_logins.pop(client_ip, None)
            session['admin'] = True
            return redirect(url_for('admin_dashboard'))
        attempts.append(now)
        admin_failed_logins[client_ip] = attempts
        flash('Invalid Username or Password', 'error')
    return render_template('admin_login.html')

@admin_bp.route('/admin/logout')
def logout():
    session.pop('admin', None)
    return redirect(url_for('login'))

@admin_bp.route('/admin/panel')
@login_required
def admin_panel(): 
    return redirect(url_for('admin_dashboard'))

@admin_bp.route('/admin/dashboard')
@login_required
def admin_dashboard():
    orders = Order.query.order_by(Order.timestamp.desc()).all()
    product_count = Product.query.count()
    pending_count = sum(1 for o in orders if o.status == 'Pending')
    return render_template(
        'admin/dashboard.html',
        orders=orders,
        product_count=product_count,
        pending_count=pending_count
    )

# --- CUSTOMER & CREDIT POINT MANAGEMENT ROUTES ---
@admin_bp.route('/admin/customers')
@login_required
def admin_customers():
    customers = CustomerUser.query.order_by(CustomerUser.created_at.desc()).all()
    customer_stats = []
    for c in customers:
        # Only calculate numbers from confirmed orders (Processing, Completed)
        confirmed_orders_query = Order.query.filter_by(customer_phone=c.phone).filter(Order.status.in_(['Processing', 'Completed']))
        order_count = confirmed_orders_query.count()
        total_spend = db.session.query(db.func.sum(Order.total_usd)).filter_by(customer_phone=c.phone).filter(Order.status.in_(['Processing', 'Completed'])).scalar() or 0.0
        customer_stats.append({
            'user': c,
            'order_count': order_count,
            'total_spend': round(float(total_spend), 2)
        })
    credit_rules = SystemSetting.get_val('credit_rules', {
        "points_per_usd": 10,
        "points_per_voucher_dollar": 20,
        "min_redeem_points": 100
    })
    return render_template('admin/customers.html', customer_stats=customer_stats, credit_rules=credit_rules)

@admin_bp.route('/admin/customers/<int:user_id>/adjust-credits', methods=['POST'])
@login_required
def adjust_customer_credits(user_id):
    user = CustomerUser.query.get_or_404(user_id)
    action = request.form.get('action', 'add').strip()
    try:
        amount = int(request.form.get('amount', 0))
    except (ValueError, TypeError):
        amount = 0
    reason = request.form.get('reason', 'Admin Adjustment').strip() or 'Admin Adjustment'

    if amount <= 0:
        flash('Please enter a valid positive number of points.', 'error')
        return redirect(url_for('admin.admin_customers'))

    adj = amount if action == 'add' else -amount
    user.credit_balance = max(0, (user.credit_balance or 0) + adj)
    tx = CreditTransaction(
        user_id=user.id,
        amount=adj,
        description=f"Admin Adjustment: {reason}"
    )
    db.session.add(tx)
    db.session.commit()
    flash(f'Successfully updated credits for {user.phone} ({adj:+d} pts). Current balance: {user.credit_balance} pts.', 'success')
    return redirect(url_for('admin.admin_customers'))

@admin_bp.route('/admin/customers/<int:user_id>/transactions')
@login_required
def customer_transactions_api(user_id):
    user = CustomerUser.query.get_or_404(user_id)
    return jsonify({
        'phone': user.phone,
        'full_name': user.full_name or '',
        'credit_balance': user.credit_balance,
        'transactions': [t.to_dict() for t in user.transactions]
    })

@admin_bp.route('/admin/customers/<int:user_id>/reset-password', methods=['POST'])
@login_required
def admin_reset_customer_password(user_id):
    user = CustomerUser.query.get_or_404(user_id)
    new_password = (request.form.get('new_password') or '').strip()
    if not new_password:
        import secrets
        new_password = str(secrets.randbelow(900000) + 100000)

    user.set_password(new_password)
    user.reset_otp = None
    user.reset_otp_expires_at = None
    db.session.commit()

    flash(f"Password for {user.phone} ({user.full_name or 'Customer'}) set to: '{new_password}'. You can now give this password to the customer.", 'success')
    return redirect(url_for('admin.admin_customers'))

# --- PROFIT ANALYSIS ROUTES (USD & KHR) ---
@admin_bp.route('/admin/profit-analysis')
@login_required
def admin_profit_analysis():
    orders = Order.query.order_by(Order.timestamp.desc()).all()
    products_by_id = {p.id: p for p in Product.query.all()}

    default_currencies = [
        {"code": "USD", "symbol": "$", "rate": 1.0, "name": "US Dollar (Base)"},
        {"code": "KHR", "symbol": "៛", "rate": 4100.0, "name": "Cambodian Riel"}
    ]
    saved_currencies = SystemSetting.get_val('currency_rates', default_currencies)
    currencies = [c for c in saved_currencies if c.get('code') in ['USD', 'KHR']] if isinstance(saved_currencies, list) else default_currencies
    if not currencies or len(currencies) < 2:
        currencies = default_currencies

    order_analysis = []
    total_revenue_usd = 0.0
    total_cost_usd = 0.0
    confirmed_orders_count = 0
    pending_orders_count = 0

    for o in orders:
        items = []
        if o.items_json:
            try:
                items = json.loads(o.items_json) if isinstance(o.items_json, str) else o.items_json
            except Exception:
                items = []

        order_cost = 0.0
        for item in items:
            qty = max(1, int(item.get('qty', 1)))
            unit_cost = None

            # 1. Use explicit cost_price recorded on item if available
            if item.get('cost_price') is not None:
                try:
                    unit_cost = float(item.get('cost_price'))
                except (ValueError, TypeError):
                    unit_cost = None

            # 2. Extract product ID from cartId (e.g. "2-0"), variantId, or id
            if unit_cost is None:
                p_id = None
                raw_id = str(item.get('id') or item.get('variantId') or item.get('cartId') or '').strip()
                if '-' in raw_id:
                    prefix = raw_id.split('-')[0].strip()
                    if prefix.isdigit():
                        p_id = int(prefix)
                elif raw_id.isdigit():
                    p_id = int(raw_id)

                p = products_by_id.get(p_id) if p_id else None

                # 3. Fallback: match by product title (e.g. "Ming for one night - Variant 1" -> "Ming for one night")
                if not p and item.get('title'):
                    raw_title = str(item.get('title')).split(' - ')[0].strip().lower()
                    for prod in products_by_id.values():
                        if prod.title and prod.title.strip().lower() == raw_title:
                            p = prod
                            break

                if p and getattr(p, 'cost_price', None) is not None:
                    unit_cost = float(p.cost_price or 0.0)
                else:
                    unit_cost = 0.0

            order_cost += unit_cost * qty

        revenue = float(o.total_usd or 0.0)
        profit = revenue - order_cost
        margin = (profit / revenue * 100.0) if revenue > 0 else 0.0

        # Calculations only include confirmed orders (Processing, Completed)
        is_confirmed = (o.status in ('Processing', 'Completed'))

        if is_confirmed:
            total_revenue_usd += revenue
            total_cost_usd += order_cost
            confirmed_orders_count += 1
        elif o.status == 'Pending':
            pending_orders_count += 1

        order_analysis.append({
            'order': o,
            'revenue_usd': revenue,
            'cost_usd': order_cost,
            'profit_usd': profit,
            'margin_pct': round(margin, 1),
            'is_confirmed': is_confirmed,
            'status': o.status
        })

    total_profit_usd = total_revenue_usd - total_cost_usd
    overall_margin = (total_profit_usd / total_revenue_usd * 100.0) if total_revenue_usd > 0 else 0.0

    # Currency summary for USD and KHR
    currency_summaries = []
    for c in currencies:
        rate = float(c.get('rate', 1.0))
        currency_summaries.append({
            'code': c.get('code', 'USD'),
            'symbol': c.get('symbol', '$'),
            'rate': rate,
            'revenue': total_revenue_usd * rate,
            'cost': total_cost_usd * rate,
            'profit': total_profit_usd * rate
        })

    return render_template(
        'admin/profit_analysis.html',
        currency_summaries=currency_summaries,
        currencies=currencies,
        total_orders=len(orders),
        confirmed_orders_count=confirmed_orders_count,
        pending_orders_count=pending_orders_count,
        total_revenue_usd=total_revenue_usd,
        total_cost_usd=total_cost_usd,
        total_profit_usd=total_profit_usd,
        overall_margin=round(overall_margin, 1),
        order_analysis=order_analysis
    )

@admin_bp.route('/admin/profit-analysis/update-rates', methods=['POST'])
@login_required
def update_currency_rates():
    try:
        c1_code = request.form.get('c1_code', 'USD').strip().upper() or 'USD'
        c1_symbol = request.form.get('c1_symbol', '$').strip() or '$'
        c1_rate = float(request.form.get('c1_rate', 1.0))

        c2_code = request.form.get('c2_code', 'KHR').strip().upper() or 'KHR'
        c2_symbol = request.form.get('c2_symbol', '៛').strip() or '៛'
        c2_rate = float(request.form.get('c2_rate', 4100.0))

        updated_rates = [
            {"code": c1_code, "symbol": c1_symbol, "rate": c1_rate, "name": f"{c1_code} (Base)"},
            {"code": c2_code, "symbol": c2_symbol, "rate": c2_rate, "name": "Cambodian Riel"}
        ]
        SystemSetting.set_val('currency_rates', updated_rates)
        flash('Currency exchange rates updated successfully!', 'success')
    except Exception as e:
        flash(f'Failed to update currency rates: {e}', 'error')

    return redirect(url_for('admin.admin_profit_analysis'))

# --- STORE & TELEGRAM SETTINGS ROUTES ---
@admin_bp.route('/admin/settings', methods=['GET', 'POST'])
@login_required
def admin_settings():
    if request.method == 'POST':
        try:
            # Credit rules
            pts_usd = int(request.form.get('points_per_usd', 10))
            pts_voucher = int(request.form.get('points_per_voucher_dollar', 20))
            min_pts = int(request.form.get('min_redeem_points', 100))

            SystemSetting.set_val('credit_rules', {
                'points_per_usd': max(1, pts_usd),
                'points_per_voucher_dollar': max(1, pts_voucher),
                'min_redeem_points': max(10, min_pts)
            })

            # Telegram config
            tg_token = request.form.get('bot_token', '').strip()
            tg_chat = request.form.get('chat_id', '').strip()
            SystemSetting.set_val('telegram_config', {
                'bot_token': tg_token,
                'chat_id': tg_chat
            })

            flash('Store settings saved successfully!', 'success')
        except Exception as e:
            flash(f'Error saving settings: {e}', 'error')

        return redirect(url_for('admin.admin_settings'))

    credit_rules = SystemSetting.get_val('credit_rules', {
        'points_per_usd': 10,
        'points_per_voucher_dollar': 20,
        'min_redeem_points': 100
    })

    tg_config = SystemSetting.get_val('telegram_config', {
        'bot_token': os.environ.get('TELEGRAM_BOT_TOKEN', '9106650406:AAEQDs96gx24HQDe3BPCArNat6lsDnoQvKQ'),
        'chat_id': os.environ.get('TELEGRAM_CHAT_ID', '')
    })

    return render_template('admin/settings.html', credit_rules=credit_rules, tg_config=tg_config)

@admin_bp.route('/admin/settings/test-telegram', methods=['POST'])
@login_required
def test_telegram_alert():
    bot_token = request.form.get('bot_token', '').strip()
    chat_id = request.form.get('chat_id', '').strip()

    try:
        from telegram_notifier import send_telegram_test_message
        ok, msg = send_telegram_test_message(bot_token, chat_id)
        if ok:
            flash(msg, 'success')
        else:
            flash(msg, 'error')
    except Exception as e:
        flash(f'Error testing Telegram: {e}', 'error')

    return redirect(url_for('admin.admin_settings'))

@admin_bp.route('/admin/inventory')
@login_required
def admin_inventory():
    # 1. Fast batch category sync (single roundtrip, no N+1 query loop)
    existing_cat_names = {c.name.strip().lower() for c in Category.query.all()}
    new_categories = []
    unique_cats = db.session.query(Product.category).distinct().all()
    for (c_name,) in unique_cats:
        if c_name:
            for sub_cat in c_name.split(','):
                sub_cat = sub_cat.strip()
                if sub_cat and sub_cat != "Other" and sub_cat.lower() not in existing_cat_names:
                    new_categories.append(Category(name=sub_cat, store="toy", sort_order=999))
                    existing_cat_names.add(sub_cat.lower())
    if new_categories:
        db.session.bulk_save_objects(new_categories)
        db.session.commit()

    products = Product.query.order_by(Product.sort_order.asc(), Product.id.desc()).all()
    categories = Category.query.order_by(Category.sort_order.asc()).all()

    # Pre-serialize cleanly to JSON for instant on-demand client rendering
    products_data = [p.to_dict() for p in products]
    products_json = json.dumps(products_data)

    # Build unique category filter list in memory
    cat_set = set()
    all_cats = []
    for c in categories:
        if c.name and (c.name.lower(), c.store) not in cat_set:
            cat_set.add((c.name.lower(), c.store))
            all_cats.append({"name": c.name, "store": c.store})
    for p in products_data:
        p_store = p["store"]
        if p["category"]:
            for sc in p["category"].split(','):
                sc = sc.strip()
                if sc and (sc.lower(), p_store) not in cat_set:
                    cat_set.add((sc.lower(), p_store))
                    all_cats.append({"name": sc, "store": p_store})
        for v in p["variants"]:
            v_cat = v.get("category", "")
            if v_cat:
                for sc in v_cat.split(','):
                    sc = sc.strip()
                    if sc and (sc.lower(), p_store) not in cat_set:
                        cat_set.add((sc.lower(), p_store))
                        all_cats.append({"name": sc, "store": p_store})

    next_product_code = generate_unique_product_code()

    return render_template(
        'admin/inventory.html',
        products=products,
        products_json=products_json,
        categories=categories,
        all_system_cats=all_cats,
        all_system_cats_json=json.dumps(all_cats),
        next_product_code=next_product_code
    )

# --- FAST LIGHTWEIGHT APIS ---
@admin_bp.route('/admin/api/next-product-code', methods=['GET'])
@login_required
def admin_api_next_product_code():
    return jsonify({"code": generate_unique_product_code()})
@admin_bp.route('/admin/api/live_stock', methods=['GET'])
@login_required
def admin_api_live_stock():
    products = Product.query.order_by(Product.sort_order.asc(), Product.id.desc()).all()
    categories = [c.name for c in Category.query.order_by(Category.sort_order.asc()).all()]
    
    items = []
    for p in products:
        p_dict = p.to_dict()
        items.append({
            "id": p_dict["id"],
            "title": p_dict["title"],
            "store": p_dict["store"],
            "image": p_dict["image"],
            "stock": p_dict["stock"],
            "category": p_dict["category"],
            "variants": [{
                "name": v.get("name", ""),
                "image": v.get("image") or p_dict["image"],
                "stock": int(v.get("stock", 0)),
                "category": v.get("category", "")
            } for v in p_dict["variants"]]
        })
        
    return jsonify({
        "products": items,
        "categories": categories
    })

@admin_bp.route('/admin/api/search_code', methods=['GET'])
@login_required
def admin_api_search_code():
    code = request.args.get('code', '').strip().upper()
    if not code: 
        return jsonify({'success': False})
    
    clean_code = code.replace('#', '').strip()
    
    # 1. Exact match on main product_code first
    p = Product.query.filter(db.func.upper(Product.product_code) == clean_code).first()
    if p:
        return jsonify({
            'success': True, 'id': str(p.id), 'title': p.title, 
            'price': p.price, 'image': p.image, 'code': p.product_code or clean_code
        })
        
    # 2. Search variants JSON for exact match on variant code
    products = Product.query.filter(Product.variants.isnot(None)).all()
    for prod in products:
        if prod.variants:
            try:
                variants = json.loads(prod.variants)
                for idx, v in enumerate(variants):
                    v_code = str(v.get('code', '')).upper().replace('#', '').strip()
                    if v_code == clean_code:
                        return jsonify({
                            'success': True, 'id': f"{prod.id}-{idx}", 
                            'title': f"{prod.title} ({v.get('name', 'Style ' + str(idx+1))})", 
                            'price': float(v.get('price', prod.price)), 
                            'image': v.get('image') or prod.image, 
                            'code': v.get('code') or clean_code
                        })
            except Exception: 
                pass
            
    # 3. Fallback to partial match on product_code
    p_partial = Product.query.filter(Product.product_code.ilike(f"%{clean_code}%")).first()
    if p_partial:
        return jsonify({
            'success': True, 'id': str(p_partial.id), 'title': p_partial.title, 
            'price': p_partial.price, 'image': p_partial.image, 'code': p_partial.product_code or clean_code
        })
            
    return jsonify({'success': False})

# --- ORDERS MANAGEMENT ---
@admin_bp.route('/admin/orders')
@login_required
def admin_orders():
    orders = Order.query.order_by(Order.timestamp.desc()).all()
    valid_orders = []
    
    for o in orders:
        try:
            o.parsed_items = json.loads(o.items_json) if o.items_json else []
        except Exception:
            o.parsed_items = []
            
        o.items = o.parsed_items
        for item in o.items:
            if 'id' not in item:
                item['id'] = item.get('cartId', item.get('variantId', str(uuid.uuid4())[:8]))
                
        o.created_at = o.timestamp or datetime.utcnow()
        o.total_amount = o.total_usd
        
        try:
            o.telegram_user = json.loads(o.telegram_user_payload) if o.telegram_user_payload else None
        except Exception:
            o.telegram_user = None

        valid_orders.append(o)
        
    return render_template('admin/orders.html', global_orders=valid_orders)

@admin_bp.route('/admin/orders/confirm/<int:id>', methods=['POST'])
@login_required
def confirm_admin_order(id):
    order = Order.query.get(id)
    if order and order.status == 'Pending':
        _deduct_order_stock(order)
        order.status = 'Processing'
        db.session.commit()
        flash('Order Confirmed and Stock Deducted!', 'success')
    return redirect(url_for('admin_orders'))

@admin_bp.route('/admin/orders/update/<int:id>', methods=['POST'])
@login_required
def update_admin_order(id):
    order = Order.query.get(id)
    if order:
        order.customer_name = request.form.get('customer_name', order.customer_name)
        order.customer_phone = request.form.get('customer_phone', order.customer_phone)
        order.customer_address = request.form.get('customer_address', order.customer_address)
        new_status = request.form.get('status', order.status)
        
        if new_status in ('Processing', 'Completed') and not order.stock_deducted:
            _deduct_order_stock(order)
            
        order.status = new_status
        
        if 'total_usd' in request.form:
            try: order.total_usd = float(request.form.get('total_usd'))
            except ValueError: pass
            
        if 'delivery_fee' in request.form:
            try: order.delivery_fee = float(request.form.get('delivery_fee'))
            except ValueError: pass
        
        item_ids = request.form.getlist('item_ids[]')
        item_qtys = request.form.getlist('item_qtys[]')
        item_prices = request.form.getlist('item_prices[]')
        item_titles = request.form.getlist('item_titles[]')
        item_images = request.form.getlist('item_images[]')
        item_styles = request.form.getlist('item_styles[]')
        item_codes = request.form.getlist('item_codes[]')
        
        if item_ids:
            rebuilt_items = []
            for i in range(len(item_ids)):
                rebuilt_items.append({
                    'id': item_ids[i],
                    'cartId': item_ids[i], 
                    'variantId': item_ids[i],
                    'title': item_titles[i] if i < len(item_titles) else 'Item',
                    'image': item_images[i] if i < len(item_images) else '',
                    'qty': int(item_qtys[i]) if (i < len(item_qtys) and str(item_qtys[i]).isdigit()) else 1,
                    'price': float(item_prices[i]) if (i < len(item_prices) and item_prices[i]) else 0.0,
                    'style': item_styles[i] if i < len(item_styles) else '',
                    'code': item_codes[i] if i < len(item_codes) else ''
                })
            order.items_json = json.dumps(rebuilt_items)
            
        db.session.commit()
        flash('Order Information Updated Successfully!', 'success')
    return redirect(url_for('admin_orders'))

@admin_bp.route('/admin/order/status/<int:id>/<string:status>', methods=['POST'])
@login_required
def update_order_status(id, status):
    order = Order.query.get(id)
    if order:
        if status in ('Processing', 'Completed') and not order.stock_deducted:
            _deduct_order_stock(order)
        order.status = status
        db.session.commit()
    return redirect(url_for('admin_orders'))

@admin_bp.route('/admin/order/delete/<int:id>', methods=['POST'])
@login_required
def delete_order(id):
    order = Order.query.get(id)
    if order:
        if order.promo_code_used:
            promo = PromoCode.query.filter_by(code=order.promo_code_used).first()
            if promo and promo.current_uses > 0:
                promo.current_uses -= 1
        db.session.delete(order)
        db.session.commit()
    return redirect(url_for('admin_orders'))

@admin_bp.route('/admin/order/bulk_delete', methods=['POST'])
@login_required
def bulk_delete_orders():
    raw_ids = request.form.get('order_ids', '')
    if raw_ids:
        ids_to_delete = [int(x) for x in raw_ids.split(',') if x.isdigit()]
        if ids_to_delete: 
            orders = Order.query.filter(Order.id.in_(ids_to_delete)).all()
            for o in orders:
                if o.promo_code_used:
                    promo = PromoCode.query.filter_by(code=o.promo_code_used).first()
                    if promo and promo.current_uses > 0:
                        promo.current_uses -= 1
                db.session.delete(o)
            db.session.commit()
    return redirect(url_for('admin_orders'))

# --- SPIN & LUCKY DRAW MANAGEMENT ---
@admin_bp.route('/admin/spin')
@login_required
def admin_spin():
    products = Product.query.filter(
        Product.is_visible == True,
        (Product.store.ilike('%toy%') | Product.store.ilike('%lego%'))
    ).order_by(Product.sort_order.asc(), Product.id.desc()).all()
    for p in products:
        try:
            p.parsed_variants = json.loads(p.variants) if p.variants else []
        except Exception:
            p.parsed_variants = []

    return render_template(
        'admin/spin.html',
        codes=RedeemCode.query.order_by(RedeemCode.timestamp.desc()).all(),
        pool=MinifigurePool.query.order_by(MinifigurePool.sort_order.asc(), MinifigurePool.id.desc()).all(),
        history=DrawHistory.query.order_by(DrawHistory.timestamp_utc.desc()).limit(100).all(),
        reward_config=get_reward_config(),
        global_products=products
    )

@admin_bp.route('/admin/redeem')
@login_required
def admin_redeem(): 
    promo_codes = PromoCode.query.order_by(PromoCode.timestamp.desc()).all()
    return render_template('admin/redeem.html', global_redeem_codes=promo_codes)

@admin_bp.route('/admin/redeem/add', methods=['POST'])
@login_required
def add_promo_code():
    code = request.form.get('code', '').strip().upper()
    if PromoCode.query.filter_by(code=code).first(): 
        flash('Code already exists!', 'error')
    else:
        db.session.add(PromoCode(
            code=code,
            discount_type=request.form.get('discount_type'),
            discount_value=float(request.form.get('discount_value', 0)),
            min_order_value=float(request.form.get('min_order_value', 0)),
            max_uses=int(request.form.get('max_uses', 0))
        ))
        db.session.commit()
    return redirect(url_for('admin_redeem'))

@admin_bp.route('/admin/redeem/toggle/<int:id>', methods=['POST'])
@login_required
def toggle_promo_code(id):
    code = PromoCode.query.get(id)
    if code: 
        code.is_active = not code.is_active
        db.session.commit()
    return redirect(url_for('admin_redeem'))

@admin_bp.route('/admin/redeem/delete/<int:id>', methods=['POST'])
@login_required
def delete_promo_code(id):
    code = PromoCode.query.get(id)
    if code: 
        db.session.delete(code)
        db.session.commit()
    return redirect(url_for('admin_redeem'))

# --- PRODUCT INVENTORY MANAGEMENT ---
@admin_bp.route('/admin/product/quick_stock', methods=['POST'])
@login_required
def quick_update_stock():
    data = request.json or {}
    p_id = data.get('product_id')
    v_idx = data.get('variant_index') 
    new_stock = int(data.get('stock', 0))

    product = Product.query.get(p_id)
    if not product: 
        return jsonify({'success': False})

    if v_idx != -1 and product.variants:
        try:
            variants = json.loads(product.variants)
            if 0 <= v_idx < len(variants):
                variants[v_idx]['stock'] = new_stock
                product.variants = json.dumps(variants)
                product.stock = sum(int(v.get('stock', 0)) for v in variants)
        except Exception:
            pass
    else: 
        product.stock = new_stock

    _sync_product_to_pool(p_id, v_idx, new_stock)
    db.session.commit()
    return jsonify({'success': True})

@admin_bp.route('/admin/categories/update', methods=['POST'])
@login_required
def update_categories():
    cat_ids = request.form.getlist('cat_ids[]')
    cat_names = request.form.getlist('cat_names[]')
    try:
        for i, cid in enumerate(cat_ids):
            cat = Category.query.get(int(cid))
            if cat:
                cat.name = cat_names[i]
                cat.sort_order = i
                file = request.files.get(f'cat_image_{cid}')
                if file and file.filename != '': 
                    cat.image = optimize_and_upload(file)['secure_url']
        db.session.commit()
    except Exception as e: 
        db.session.rollback()
        print(f"Update Category error: {e}")
    return redirect(url_for('admin_inventory'))

@admin_bp.route('/admin/category/delete/<int:id>', methods=['POST'])
@login_required
def delete_category(id):
    c = Category.query.get(id)
    if c:
        products = Product.query.filter_by(category=c.name).all()
        for p in products: 
            p.category = "Other"
        db.session.delete(c)
        db.session.commit()
    return redirect(url_for('admin_inventory'))

@admin_bp.route('/admin/product/reorder', methods=['POST'])
@admin_bp.route('/admin/products/reorder', methods=['POST'])
@login_required
def admin_reorder_products():
    try:
        if request.is_json:
            data = request.get_json()
            product_ids = data.get('product_ids', data.get('ids', []))
        else:
            order_data = request.form.get('product_order')
            if not order_data:
                return jsonify({"status": "error", "message": "No data provided"}), 400
            product_ids = json.loads(order_data)

        for index, prod_id in enumerate(product_ids):
            product = Product.query.get(int(prod_id))
            if product:
                product.sort_order = index
                
        db.session.commit()
        return jsonify({"status": "success", "message": "Product sort order updated successfully"}), 200
        
    except Exception as e:
        db.session.rollback()
        print(f"Reorder Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@admin_bp.route('/admin/product/toggle/<int:id>', methods=['POST'])
@login_required
def toggle_product(id):
    p = Product.query.get(id)
    if p: 
        p.is_visible = not p.is_visible
        db.session.commit()
    return redirect(url_for('admin_inventory'))

@admin_bp.route('/admin/product/delete/<int:id>', methods=['POST'])
@login_required
def delete_product(id):
    p = Product.query.get(id)
    if p: 
        db.session.delete(p)
        db.session.commit()
    return redirect(url_for('admin_inventory'))

@admin_bp.route('/admin/product/update/<int:id>', methods=['POST'])
@login_required
def update_product(id):
    p = Product.query.get_or_404(id)
    try:
        new_code = request.form.get('product_code', '').strip().upper()
        if not new_code:
            if not p.product_code:
                p.product_code = generate_unique_product_code()
        else:
            conflict = Product.query.filter(
                db.func.upper(Product.product_code) == new_code,
                Product.id != p.id
            ).first()
            if not conflict:
                p.product_code = new_code
            elif not p.product_code:
                p.product_code = generate_unique_product_code()

        p.title = request.form.get('title')
        p.category = request.form.get('category')
        stores = request.form.getlist('stores[]')
        p.store = ",".join(stores) if stores else "toy"
        p.discount_percent = _safe_float(request.form.get('discount_percent'), 0.0)
        
        # Fully editable Cost Price (Original) and Selling Price
        if request.form.get('cost_price') is not None:
            p.cost_price = _safe_float(request.form.get('cost_price'), 0.0)
        if request.form.get('price') is not None and str(request.form.get('price')).strip() != '':
            p.price = _safe_float(request.form.get('price'), 0.0)

        # Editable Stock Quantity directly set by admin
        req_stock = request.form.get('stock')
        user_stock = _safe_int(req_stock, None) if (req_stock is not None and str(req_stock).strip() != '') else None
        
        use_custom_thumb_val = request.form.get('use_custom_thumbnail')
        p.use_custom_thumbnail = True if str(use_custom_thumb_val).lower() == 'true' else False
        
        thumb_file = request.files.get('thumbnail')
        if p.use_custom_thumbnail and thumb_file and thumb_file.filename != '':
            try:
                upload_res = optimize_and_upload(thumb_file)
                if upload_res and 'secure_url' in upload_res:
                    p.image = upload_res['secure_url']
            except Exception as e:
                print(f"Error uploading thumbnail: {e}")

        v_ids = request.form.getlist('v_ids[]')
        v_images = request.form.getlist('v_images[]')
        v_names = request.form.getlist('v_names[]')
        v_prices = request.form.getlist('v_prices[]')
        v_stocks = request.form.getlist('v_stocks[]')
        v_cats = request.form.getlist('v_categories[]')
        v_discounts = request.form.getlist('v_discounts[]') 
        v_codes = request.form.getlist('v_codes[]')
        
        updated_variants = []
        total_stock = 0
        for i in range(len(v_ids)):
            raw_vid = v_ids[i] if i < len(v_ids) else None
            vid = _safe_int(raw_vid, i)
            v_stock_raw = v_stocks[i] if i < len(v_stocks) else 0
            stock = _safe_int(v_stock_raw, 0)
            v_pr = _safe_float(v_prices[i], p.price) if i < len(v_prices) else p.price
            
            # If admin edited main price and there is only 1 variant or variant price was 0, sync it
            if request.form.get('price') and (v_pr == 0.0 or len(v_ids) == 1):
                v_pr = p.price

            # If admin explicitly entered a main stock and there is only 1 variant, sync it
            if user_stock is not None and len(v_ids) == 1:
                stock = user_stock

            updated_variants.append({
                "id": vid, 
                "image": v_images[i] if i < len(v_images) else p.image, 
                "name": v_names[i] if i < len(v_names) else f"Style {i+1}",
                "price": v_pr, 
                "stock": stock, 
                "category": v_cats[i] if i < len(v_cats) else p.category,
                "discount_percent": _safe_float(v_discounts[i], 0.0) if i < len(v_discounts) else 0.0,
                "code": v_codes[i] if i < len(v_codes) else ''
            })
            total_stock += stock
            _sync_product_to_pool(p.id, vid, stock)

        new_files = request.files.getlist('new_images')
        if new_files and new_files[0].filename != '':
            last_id = max([v['id'] for v in updated_variants]) if updated_variants else 0
            for f in new_files:
                if f and f.filename != '':
                    try:
                        res = optimize_and_upload(f)
                        last_id += 1
                        updated_variants.append({
                            "id": last_id, "name": f"New Style {last_id}", "price": updated_variants[0]['price'] if updated_variants else p.price, 
                            "stock": 1, "image": res['secure_url'], "category": p.category, "discount_percent": 0.0,
                            "code": ""
                        })
                        total_stock += 1
                    except Exception as e: 
                        print(f"Error adding new variant image: {e}")
        
        d_list = []
        try: 
            if getattr(p, 'detail_images', None): 
                d_list = json.loads(p.detail_images)
        except Exception:
            pass

        new_detail_files = request.files.getlist('new_detail_images')
        if new_detail_files and new_detail_files[0].filename != '':
            for df in new_detail_files:
                if df and df.filename != '':
                    try:
                        upload_result = optimize_and_upload(df)
                        if upload_result and 'secure_url' in upload_result:
                            d_list.append(upload_result['secure_url'])
                    except Exception as e:
                        print(f"Error uploading detail image: {e}")
        p.detail_images = json.dumps(d_list)

        if updated_variants:
            p.variants = json.dumps(updated_variants)
            if not p.use_custom_thumbnail:
                p.image = updated_variants[0]['image']
            if request.form.get('price') is not None and str(request.form.get('price')).strip() != '':
                p.price = _safe_float(request.form.get('price'), p.price)
            else:
                p.price = updated_variants[0]['price']
            p.stock = total_stock if user_stock is None else user_stock
        else:
            if user_stock is not None:
                p.stock = user_stock

        db.session.commit()
        flash(f'Product "{p.title}" updated successfully!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error updating product: {e}', 'error')
    return redirect(url_for('admin_inventory'))

@admin_bp.route('/admin/product/add', methods=['POST'])
@login_required
def add_product():
    product_code_raw = request.form.get('product_code', '').strip().upper()
    if not product_code_raw:
        product_code_raw = generate_unique_product_code()
    else:
        existing = Product.query.filter(db.func.upper(Product.product_code) == product_code_raw).first()
        if existing:
            product_code_raw = generate_unique_product_code()
    title = request.form.get('title')
    category = request.form.get('category')
    stores = request.form.getlist('stores[]')
    store_str = ",".join(stores) if stores else "toy"
    discount_percent = float(request.form.get('discount_percent', 0.0) or 0.0)
    
    use_custom_thumb = str(request.form.get('use_custom_thumbnail')).lower() == 'true'
    thumb_file = request.files.get('thumbnail')
    thumbnail_url = ""
    
    if use_custom_thumb and thumb_file and thumb_file.filename != '':
        try:
            res = optimize_and_upload(thumb_file)
            if res and 'secure_url' in res:
                thumbnail_url = res['secure_url']
        except Exception as e: 
            print(f"Upload error: {e}")
    
    v_names = request.form.getlist('variant_names[]')
    v_prices = request.form.getlist('variant_prices[]')
    v_stocks = request.form.getlist('variant_stocks[]')
    v_categories = request.form.getlist('variant_categories[]')
    v_discounts = request.form.getlist('variant_discounts[]')
    v_codes = request.form.getlist('variant_codes[]')
    
    files = request.files.getlist('images')
    uploaded_urls = []
    for f in files:
        if f and f.filename != '': 
            try: 
                uploaded_urls.append(optimize_and_upload(f)['secure_url'])
            except Exception: 
                pass
    
    detail_files = request.files.getlist('detail_images')
    detail_urls = []
    if detail_files and detail_files[0].filename != '':
        for df in detail_files:
            if df and df.filename != '': 
                try: 
                    detail_urls.append(optimize_and_upload(df)['secure_url'])
                except Exception: 
                    pass

    cost_price = _safe_float(request.form.get('cost_price'), 0.0)
    selling_price_input = _safe_float(request.form.get('price'), 0.0)
    req_stock = _safe_int(request.form.get('stock'), 1)

    try:
        new_p = None
        if uploaded_urls:
            vars_json = []
            total_stock = 0
            for i, url in enumerate(uploaded_urls):
                price = _safe_float(v_prices[i], selling_price_input) if (i < len(v_prices) and _safe_float(v_prices[i], 0.0) > 0) else selling_price_input
                stock = _safe_int(v_stocks[i], 0) if i < len(v_stocks) else 0
                cat_str = v_categories[i] if i < len(v_categories) else category
                v_disc = _safe_float(v_discounts[i], 0.0) if i < len(v_discounts) else 0.0
                v_code = v_codes[i] if i < len(v_codes) else ''
                
                vars_json.append({
                    "id": i, "name": v_names[i] if i < len(v_names) else f"Style {i+1}", 
                    "price": price, "stock": stock, "image": url, 
                    "category": cat_str, "discount_percent": v_disc, "code": v_code
                })
                total_stock += stock

            if len(uploaded_urls) == 1 and req_stock > 0 and total_stock <= 1:
                vars_json[0]['stock'] = req_stock
                total_stock = req_stock
                
            if not use_custom_thumb or not thumbnail_url:
                thumbnail_url = uploaded_urls[0]
                
            final_price = selling_price_input if selling_price_input > 0 else (vars_json[0]['price'] if vars_json else 0.0)
            new_p = Product(
                product_code=product_code_raw,
                title=title or f"Product #{product_code_raw or ''}", 
                price=final_price,
                cost_price=cost_price,
                stock=total_stock, 
                image=thumbnail_url, 
                category=category or "General", 
                store=store_str, 
                discount_percent=discount_percent, 
                variants=json.dumps(vars_json), 
                sort_order=-1, 
                is_visible=True,
                use_custom_thumbnail=use_custom_thumb,
                detail_images=json.dumps(detail_urls)
            )
            db.session.add(new_p)
            db.session.commit()
            flash(f'Product "{new_p.title}" published successfully!', 'success')
        elif thumbnail_url or thumb_file:
            new_p = Product(
                product_code=product_code_raw,
                title=title or f"Product #{product_code_raw or ''}",
                price=selling_price_input,
                cost_price=cost_price,
                stock=req_stock,
                image=thumbnail_url or "/static/images/hero-bg.jpg",
                category=category or "General",
                store=store_str,
                discount_percent=discount_percent,
                variants=json.dumps([{
                    "id": 0, "name": "Default Style", "price": selling_price_input,
                    "stock": req_stock, "image": thumbnail_url or "/static/images/hero-bg.jpg",
                    "category": category or "General", "discount_percent": discount_percent,
                    "code": product_code_raw
                }]),
                sort_order=-1,
                is_visible=True,
                use_custom_thumbnail=use_custom_thumb,
                detail_images=json.dumps(detail_urls)
            )
            db.session.add(new_p)
            db.session.commit()
            flash(f'Product "{new_p.title}" published successfully!', 'success')
        elif title or product_code_raw or selling_price_input > 0:
            fallback_img = "/static/images/hero-bg.jpg"
            new_p = Product(
                product_code=product_code_raw,
                title=title or f"Product #{product_code_raw or ''}",
                price=selling_price_input,
                cost_price=cost_price,
                stock=req_stock,
                image=fallback_img,
                category=category or "General",
                store=store_str,
                discount_percent=discount_percent,
                variants=json.dumps([{
                    "id": 0, "name": "Default Style", "price": selling_price_input,
                    "stock": req_stock, "image": fallback_img,
                    "category": category or "General", "discount_percent": discount_percent,
                    "code": product_code_raw
                }]),
                sort_order=-1,
                is_visible=True,
                use_custom_thumbnail=False,
                detail_images=json.dumps(detail_urls)
            )
            db.session.add(new_p)
            db.session.commit()
            flash(f'Product "{new_p.title}" published successfully!', 'success')
        else:
            flash('Please provide at least a title or upload images to add a product.', 'error')
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to publish product: {e}', 'error')
    return redirect(url_for('admin_inventory'))

# --- PRIZE POOL & REDEEM CODES ---
@admin_bp.route('/admin/spin/update_rewards', methods=['POST'])
@login_required
def update_spin_rewards():
    data = {
        "0": float(request.form.get("pct_0", 50)),
        "500": float(request.form.get("pct_500", 20)),
        "1000": float(request.form.get("pct_1000", 15)),
        "2000": float(request.form.get("pct_2000", 10)),
        "5000": float(request.form.get("pct_5000", 3)),
        "10000": float(request.form.get("pct_10000", 1.5)),
        "50000": float(request.form.get("pct_50000", 0.5))
    }
    save_reward_config(data)
    flash('5x Spin Probabilities updated!', 'success')
    return redirect(url_for('admin_spin'))

@admin_bp.route('/admin/spin/generate_codes', methods=['POST'])
@login_required
def generate_codes():
    quantity = int(request.form.get('quantity', 5))
    value = int(request.form.get('value', 1000))
    for _ in range(quantity): 
        db.session.add(RedeemCode(code=''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(8)), value=value))
    db.session.commit()
    return redirect(url_for('admin_spin'))

@admin_bp.route('/admin/spin/add_pool_catalog', methods=['POST'])
@login_required
def add_pool_catalog():
    if request.is_json:
        data = request.get_json() or {}
        items = data.get('catalog_items', [])
        rarity = data.get('rarity', 'Common')
    else:
        items = request.form.getlist('catalog_items[]')
        rarity = request.form.get('rarity', 'Common')

    added_count = 0
    for item_data in items:
        parts = str(item_data).split('|')
        if len(parts) == 2 and parts[0].isdigit():
            p_id = int(parts[0])
            v_idx = int(parts[1]) if parts[1].lstrip('-').isdigit() else -1
            product = Product.query.get(p_id)
            if product:
                image, name, stock = product.image, product.title, product.stock
                if v_idx != -1 and product.variants:
                    try:
                        variants = json.loads(product.variants)
                        if 0 <= v_idx < len(variants):
                            image = variants[v_idx].get('image', image)
                            name = f"{variants[v_idx].get('name', 'Variant')} {product.title}"
                            stock = variants[v_idx].get('stock', 0)
                    except Exception:
                        pass
                if not MinifigurePool.query.filter_by(linked_product_id=p_id, linked_variant_index=v_idx).first():
                    db.session.add(MinifigurePool(name=name, rarity=rarity, stock=stock, image=image, linked_product_id=p_id, linked_variant_index=v_idx))
                    added_count += 1
    if added_count > 0:
        db.session.commit()

    if request.is_json or request.headers.get('Accept') == 'application/json':
        return jsonify({"status": "success", "added": added_count})
    return redirect(url_for('admin_spin'))

@admin_bp.route('/admin/spin/add_pool', methods=['POST'])
@login_required
def add_spin_pool():
    rarity = request.form.get('rarity')
    stock = int(request.form.get('stock', 1))
    for file in request.files.getlist('images'):
        if file and file.filename != '': 
            db.session.add(MinifigurePool(name=request.form.get('name', '').strip() or f"Mystery {rarity} Prize", rarity=rarity, stock=stock, image=optimize_and_upload(file)['secure_url']))
    db.session.commit()
    return redirect(url_for('admin_spin'))

@admin_bp.route('/admin/spin/update_stock/<int:id>', methods=['POST'])
@login_required
def update_spin_stock(id):
    item = MinifigurePool.query.get(id)
    if item:
        item.stock = int(request.form.get('stock', 0))
        _sync_pool_to_product(item)
        db.session.commit()
    return redirect(url_for('admin_spin'))

@admin_bp.route('/admin/spin/pool/update_bulk', methods=['POST'])
@login_required
def admin_spin_update_bulk():
    data = request.json or {}
    item = MinifigurePool.query.get(data.get('id'))
    if item:
        item.rarity = data.get('rarity', item.rarity)
        item.sort_order = int(data.get('sort_order', item.sort_order))
        item.stock = int(data.get('stock', item.stock))
        _sync_pool_to_product(item)
        db.session.commit()
    return jsonify({"status": "success"})

@admin_bp.route('/admin/spin/pool/update_order/<int:item_id>', methods=['POST'])
@login_required
def update_spin_pool_order(item_id):
    MinifigurePool.query.get_or_404(item_id).sort_order = int(request.form.get('sort_order', 0))
    db.session.commit()
    return redirect(url_for('admin_spin'))

@admin_bp.route('/admin/spin/pool/update_rarity/<int:item_id>', methods=['POST'])
@login_required
def admin_spin_update_rarity(item_id):
    MinifigurePool.query.get_or_404(item_id).rarity = request.form.get('rarity')
    db.session.commit()
    return redirect(url_for('admin_spin'))

@admin_bp.route('/admin/spin/pool/bulk_delete', methods=['POST'])
@login_required
def admin_spin_bulk_delete_pool():
    if request.form.get('item_ids'):
        MinifigurePool.query.filter(MinifigurePool.id.in_([int(x) for x in request.form.get('item_ids').split(',') if x.isdigit()])).delete(synchronize_session=False)
        db.session.commit()
    return redirect(url_for('admin_spin'))

@admin_bp.route('/admin/spin/code/bulk_delete', methods=['POST'])
@login_required
def admin_spin_bulk_delete_code():
    if request.form.get('code_ids'):
        RedeemCode.query.filter(RedeemCode.id.in_([int(x) for x in request.form.get('code_ids').split(',') if x.isdigit()])).delete(synchronize_session=False)
        db.session.commit()
    return redirect(url_for('admin_spin'))

@admin_bp.route('/admin/spin/history/bulk_delete', methods=['POST'])
@login_required
def admin_spin_bulk_delete_history():
    if request.form.get('history_ids'):
        DrawHistory.query.filter(DrawHistory.id.in_([int(x) for x in request.form.get('history_ids').split(',') if x.isdigit()])).delete(synchronize_session=False)
        db.session.commit()
    return redirect(url_for('admin_spin'))

@admin_bp.route('/admin/spin/pool/delete/<int:item_id>', methods=['POST'])
@login_required
def admin_spin_delete_pool(item_id):
    db.session.delete(MinifigurePool.query.get_or_404(item_id))
    db.session.commit()
    return redirect(url_for('admin_spin'))

@admin_bp.route('/admin/spin/history/delete/<int:draw_id>', methods=['POST'])
@login_required
def admin_spin_delete_history(draw_id):
    db.session.delete(DrawHistory.query.get_or_404(draw_id))
    db.session.commit()
    return redirect(url_for('admin_spin'))
