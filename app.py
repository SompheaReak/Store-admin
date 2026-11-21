import os
import json
from datetime import timedelta
from flask import Flask, render_template, redirect, url_for, request, session, jsonify
import cloudinary
import cloudinary.uploader

# Load .env variables
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE_DIR, '.env'))
except ImportError:
    pass

def _load_env_fallback():
    env_path = os.path.join(BASE_DIR, '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
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
    api_key=os.environ.get("CLOUDINARY_API_KEY", "998136922585551"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", "mL1MT4pxmbzUiWsW9BagJNtlhh8"),
    secure=True
)

from models import db, Product, Category, PromoCode, Order
from admin_routes import admin_bp

app = Flask(__name__)

# Security & Session Settings
app.secret_key = os.environ.get('SECRET_KEY', 'somphea_reak_ultra_pro_2025')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
if os.environ.get('FLASK_ENV') == 'production' or os.environ.get('RENDER') or os.environ.get('DYNO'):
    app.config['SESSION_COOKIE_SECURE'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)

# Database Configuration (Point to the same Database as the storefront)
DEFAULT_NEON_DB = "postgresql://neondb_owner:npg_dq6KIUVe0cyS@ep-polished-feather-a19qzv4s-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require"
db_url = os.environ.get('DATABASE_URL', '').strip()
if not db_url or 'fallback.db' in db_url:
    db_url = DEFAULT_NEON_DB

if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

# Clean channel_binding parameter as it causes libpq/SCRAM failures in certain deployment environments
db_url = db_url.replace("&channel_binding=require", "").replace("?channel_binding=require&", "?").replace("?channel_binding=require", "")

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
        if request.path.startswith('/admin/api/') or request.path.startswith('/admin/product/quick_stock'):
            return dict()
        try:
            categories = Category.query.order_by(Category.sort_order).all()
            pending_count = Order.query.filter_by(status='Pending').count()
            return dict(global_categories=categories, pending_count=pending_count)
        except Exception as e:
            print(f"Warning in inject_global_data: {e}")
            return dict()
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

@app.route('/admin/db-status')
def db_status():
    if not session.get('admin'):
        return jsonify({"error": "unauthorized"}), 401
    
    db_uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    masked_uri = db_uri
    if '@' in db_uri:
        prefix = db_uri.split('@')[0]
        host = db_uri.split('@')[1]
        masked_uri = f"{prefix.split(':')[0]}://****@{host}"
        
    try:
        from models import Product, Category, Order
        prod_count = Product.query.count()
        cat_count = Category.query.count()
        order_count = Order.query.count()
        first_prod = Product.query.first()
        return jsonify({
            "status": "connected",
            "db_type": "postgres" if "postgres" in db_uri else "sqlite",
            "masked_uri": masked_uri,
            "product_count": prod_count,
            "category_count": cat_count,
            "order_count": order_count,
            "sample_product": first_prod.title if first_prod else None
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "db_uri": masked_uri,
            "error": str(e)
        }), 500

# Security headers middleware
@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    return response

with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        print(f"Warning during DB startup: {e}")

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5001)))
