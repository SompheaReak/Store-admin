import os
import json
from datetime import datetime, timedelta
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

# --- 3. STORE MODELS ---
class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_code = db.Column(db.String(50), nullable=True) # Product SKU Code
    title = db.Column(db.String(200), nullable=False)
    price = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, default=0) 
    image = db.Column(db.String(500), nullable=False) 
    category = db.Column(db.String(100), nullable=False)
    store = db.Column(db.String(100), nullable=False) 
    variants = db.Column(db.Text, nullable=True)
    sort_order = db.Column(db.Integer, default=0)
    is_visible = db.Column(db.Boolean, default=True)
    discount_percent = db.Column(db.Float, default=0.0) 
    use_custom_thumbnail = db.Column(db.Boolean, default=False)
    detail_images = db.Column(db.Text, nullable=True)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    store = db.Column(db.String(50), nullable=False)
    image = db.Column(db.String(500), nullable=True, default="https://via.placeholder.com/150?text=Upload")
    sort_order = db.Column(db.Integer, default=0)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    customer_name = db.Column(db.String(100), nullable=False)
    customer_phone = db.Column(db.String(20), nullable=False)
    customer_address = db.Column(db.Text, nullable=False)
    items_json = db.Column(db.Text, nullable=False) 
    total_usd = db.Column(db.Float, nullable=False)
    delivery_fee = db.Column(db.Float, default=0.0)
    status = db.Column(db.String(20), default="Pending")
    stock_deducted = db.Column(db.Boolean, default=False)
    promo_code_used = db.Column(db.String(50), nullable=True)
    
    # NEW TELEGRAM COLUMNS FOR CART SYNC
    telegram_id = db.Column(db.String(100), nullable=True)
    telegram_name = db.Column(db.String(200), nullable=True)
    telegram_user_payload = db.Column(db.Text, nullable=True) 
    
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class PromoCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    discount_type = db.Column(db.String(20), nullable=False) 
    discount_value = db.Column(db.Float, nullable=False)
    min_order_value = db.Column(db.Float, default=0.0)
    max_uses = db.Column(db.Integer, default=0)
    current_uses = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class PlayerSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.String(8), unique=True, nullable=False) 
    balance = db.Column(db.Integer, default=0)

class RedeemCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(8), unique=True, nullable=False)
    value = db.Column(db.Integer, nullable=False)  
    status = db.Column(db.String(20), default="Active") 
    redeemed_by = db.Column(db.String(8), nullable=True) 
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class MinifigurePool(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    image = db.Column(db.String(500), nullable=False)
    rarity = db.Column(db.String(50), nullable=False)
    stock = db.Column(db.Integer, default=0)
    sort_order = db.Column(db.Integer, default=0)
    linked_product_id = db.Column(db.Integer, nullable=True)
    linked_variant_index = db.Column(db.Integer, nullable=True)

class DrawHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.String(8), nullable=False)
    item_name = db.Column(db.String(200), nullable=False)
    rarity = db.Column(db.String(50), nullable=False)
    stock_remaining = db.Column(db.Integer, nullable=False)
    timestamp_utc = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def cambodia_time(self):
        kh_time = self.timestamp_utc + timedelta(hours=7)
        return kh_time.strftime('%d-%b-%Y %I:%M %p')

REWARD_CONFIG_FILE = 'spin_rewards.json'

def get_reward_config():
    if os.path.exists(REWARD_CONFIG_FILE):
        with open(REWARD_CONFIG_FILE, 'r') as f: 
            return json.load(f)
    return {"0": 50.0, "500": 20.0, "1000": 15.0, "2000": 10.0, "5000": 3.0, "10000": 1.5, "50000": 0.5}

def save_reward_config(data):
    with open(REWARD_CONFIG_FILE, 'w') as f: 
        json.dump(data, f)

def _sync_product_to_pool(product_id, variant_index, new_stock):
    linked_prize = MinifigurePool.query.filter_by(linked_product_id=product_id, linked_variant_index=variant_index).first()
    if linked_prize: 
        linked_prize.stock = new_stock
    else:
        product = Product.query.get(product_id)
        if product:
            target_image = product.image
            if variant_index != -1 and product.variants:
                try:
                    variants = json.loads(product.variants)
                    if 0 <= variant_index < len(variants): 
                        target_image = variants[variant_index].get('image', target_image)
                except Exception as e: 
                    print(f"Error parsing variants: {e}")
            
            prize = MinifigurePool.query.filter_by(image=target_image).first()
            if prize:
                prize.stock = new_stock
                prize.linked_product_id = product_id
                prize.linked_variant_index = variant_index

def _sync_pool_to_product(pool_item):
    if pool_item.linked_product_id is not None:
        product = Product.query.get(pool_item.linked_product_id)
        if product:
            if pool_item.linked_variant_index != -1 and product.variants:
                try:
                    variants = json.loads(product.variants)
                    if 0 <= pool_item.linked_variant_index < len(variants):
                        variants[pool_item.linked_variant_index]['stock'] = pool_item.stock
                        product.variants = json.dumps(variants)
                        product.stock = sum(int(v.get('stock', 0)) for v in variants)
                except Exception as e: 
                    print(f"Error updating product variants: {e}")
            else: 
                product.stock = pool_item.stock
    else:
        products = Product.query.all()
        for p in products:
            if p.variants:
                try:
                    variants = json.loads(p.variants)
                    updated = False
                    for idx, v in enumerate(variants):
                        if v.get('image') == pool_item.image:
                            variants[idx]['stock'] = pool_item.stock
                            updated = True
                            pool_item.linked_product_id = p.id
                            pool_item.linked_variant_index = idx
                    if updated:
                        p.variants = json.dumps(variants)
                        p.stock = sum(int(v.get('stock', 0)) for v in variants)
                        break
                except Exception: 
                    pass
            elif p.image == pool_item.image:
                p.stock = pool_item.stock
                pool_item.linked_product_id = p.id
                pool_item.linked_variant_index = -1
                break
