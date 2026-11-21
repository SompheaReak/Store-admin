import os
import json
import random
import string
import uuid
import hmac
from datetime import datetime
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, jsonify, session, flash
from models import (
    db, Product, Category, Order, PromoCode, PlayerSession,
    RedeemCode, MinifigurePool, DrawHistory, get_reward_config,
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

def optimize_and_upload(file):
    return cloudinary.uploader.upload(
        file, format="webp", quality="auto", width=900, height=900, crop="limit"
    )

admin_bp = Blueprint('admin', __name__, template_folder='templates')

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'Thesong_Admin@2022?!$')

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
                v_idx = int(parts[1]) if parts[1].isdigit() else -1
                product = Product.query.get(p_id)
                if product:
                    if product.variants and v_idx != -1:
                        variants = json.loads(product.variants)
                        if 0 <= v_idx < len(variants):
                            variants[v_idx]['stock'] = max(0, int(variants[v_idx].get('stock', 0)) - qty)
                            product.variants = json.dumps(variants)
                            product.stock = sum(int(v.get('stock', 0)) for v in variants)
                            _sync_product_to_pool(p_id, v_idx, variants[v_idx]['stock'])
                    else: 
                        product.stock = max(0, product.stock - qty)
                        _sync_product_to_pool(p_id, -1, product.stock)
            elif identifier.isdigit():
                p_id = int(identifier)
                product = Product.query.get(p_id)
                if product:
                    product.stock = max(0, product.stock - qty)
                    _sync_product_to_pool(p_id, -1, product.stock)
    except Exception as e:
        print(f"Stock deduction error for order {order.id}: {e}")

    if order.promo_code_used:
        promo = PromoCode.query.filter_by(code=order.promo_code_used).first()
        if promo:
            promo.current_uses += 1
    order.stock_deducted = True

# --- ADMIN AUTH & NAVIGATION ROUTES ---
@admin_bp.route('/admin/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user_input = request.form.get('username', '')
        pass_input = request.form.get('password', '')
        if hmac.compare_digest(user_input, ADMIN_USERNAME) and hmac.compare_digest(pass_input, ADMIN_PASS):
            session['admin'] = True
            return redirect(url_for('admin_dashboard'))
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

    return render_template(
        'admin/inventory.html',
        products=products,
        products_json=products_json,
        categories=categories,
        all_system_cats=all_cats,
        all_system_cats_json=json.dumps(all_cats)
    )

# --- FAST LIGHTWEIGHT APIS ---
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
            Order.query.filter(Order.id.in_(ids_to_delete)).delete(synchronize_session=False)
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
    
    p.product_code = request.form.get('product_code', '').strip().upper()
    p.title = request.form.get('title')
    p.category = request.form.get('category')
    stores = request.form.getlist('stores[]')
    p.store = ",".join(stores) if stores else "toy"
    p.discount_percent = float(request.form.get('discount_percent', 0.0) or 0.0)
    
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
        stock = int(v_stocks[i]) if i < len(v_stocks) else 0
        updated_variants.append({
            "id": int(v_ids[i]), 
            "image": v_images[i] if i < len(v_images) else p.image, 
            "name": v_names[i] if i < len(v_names) else f"Style {i+1}",
            "price": float(v_prices[i]) if i < len(v_prices) else p.price, 
            "stock": stock, 
            "category": v_cats[i] if i < len(v_cats) else p.category,
            "discount_percent": float(v_discounts[i]) if i < len(v_discounts) else 0.0,
            "code": v_codes[i] if i < len(v_codes) else ''
        })
        total_stock += stock
        _sync_product_to_pool(p.id, int(v_ids[i]), stock)

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

    p.variants = json.dumps(updated_variants)
    p.stock = total_stock
    if updated_variants:
        if not p.use_custom_thumbnail:
            p.image = updated_variants[0]['image']
        p.price = updated_variants[0]['price']
    db.session.commit()
    return redirect(url_for('admin_inventory'))

@admin_bp.route('/admin/product/add', methods=['POST'])
@login_required
def add_product():
    product_code_raw = request.form.get('product_code', '').strip().upper()
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

    if uploaded_urls:
        vars_json = []
        total_stock = 0
        for i, url in enumerate(uploaded_urls):
            price = float(v_prices[i]) if i < len(v_prices) else 0.0
            stock = int(v_stocks[i]) if i < len(v_stocks) else 0
            cat_str = v_categories[i] if i < len(v_categories) else category
            v_disc = float(v_discounts[i]) if i < len(v_discounts) else 0.0
            v_code = v_codes[i] if i < len(v_codes) else ''
            
            vars_json.append({
                "id": i, "name": v_names[i] if i < len(v_names) else f"Style {i+1}", 
                "price": price, "stock": stock, "image": url, 
                "category": cat_str, "discount_percent": v_disc, "code": v_code
            })
            total_stock += stock
            
        if not use_custom_thumb or not thumbnail_url:
            thumbnail_url = uploaded_urls[0]
            
        new_p = Product(
            product_code=product_code_raw,
            title=title or f"Product #{product_code_raw or ''}", 
            price=vars_json[0]['price'], 
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
