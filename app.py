import os
import json
from datetime import timedelta
from flask import Flask, render_template, redirect, url_for, request, session
import cloudinary
import cloudinary.uploader

# Load .env variables
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

# Cloudinary config
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", "dwwearehy"),
    api_key=os.environ.get("CLOUDINARY_API_KEY", "dummy_key"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", "dummy_secret"),
    secure=True
)

from models import db, Product, Category, PromoCode, Order
from admin_routes import admin_bp

app = Flask(__name__)

# Security & Session Settings
app.secret_key = os.environ.get('SECRET_KEY', 'somphea_reak_admin_ultra_pro_2026')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get('FLASK_ENV') == 'production' or os.environ.get('RENDER') or os.environ.get('DYNO'):
    app.config['SESSION_COOKIE_SECURE'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)

# Database Configuration (Point to the same Database as the storefront)
db_url = os.environ.get('DATABASE_URL', 'sqlite:///fallback.db')
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

if 'sqlite' not in db_url:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_pre_ping": True, "pool_recycle": 300, "pool_timeout": 30
    }
else:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_pre_ping": True
    }

db.init_app(app)

# Global context processor for admin UI
@app.context_processor
def inject_global_data():
    if request.path.startswith('/admin') and session.get('admin'):
        products = Product.query.order_by(Product.sort_order.asc(), Product.id.desc()).all()
        for p in products: 
            p.parsed_variants = json.loads(p.variants) if p.variants else []
        categories = Category.query.order_by(Category.sort_order).all()
        promo_codes = PromoCode.query.order_by(PromoCode.timestamp.desc()).all()
        pending_count = Order.query.filter_by(status='Pending').count()
        return dict(global_products=products, global_categories=categories, global_redeem_codes=promo_codes, pending_count=pending_count)
    return dict()

# Helper for blueprint URL lookups
def _url_build_error_handler(error, endpoint, values):
    for ep in app.view_functions:
        if ep.endswith('.' + endpoint):
            return url_for(ep, **values)
    raise error

app.url_build_error_handlers.append(_url_build_error_handler)

# Register Admin Blueprint
app.register_blueprint(admin_bp)

# Root route redirects to admin login / dashboard
@app.route('/')
def home():
    if session.get('admin'):
        return redirect(url_for('admin_dashboard'))
    return redirect(url_for('login'))

# Security headers middleware
@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    return response

with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"Warning during DB startup: {e}")

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
