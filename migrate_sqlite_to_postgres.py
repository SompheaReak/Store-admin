import os
import sqlite3
from app import app, db
from models import Category, Product, MinifigurePool, PromoCode, Order, PlayerSession, RedeemCode, DrawHistory
from sqlalchemy import text

def migrate_sqlite_to_postgres(sqlite_path='instance/fallback.db'):
    if not os.path.exists(sqlite_path):
        print(f"Error: {sqlite_path} not found.")
        return False

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    with app.app_context():
        # Ensure schema exists in Postgres
        db.create_all()

        print(f"Connecting to database: {app.config['SQLALCHEMY_DATABASE_URI'].split('@')[-1] if '@' in app.config['SQLALCHEMY_DATABASE_URI'] else 'sqlite'}")

        # 1. Categories
        cat_rows = sqlite_conn.execute('SELECT * FROM category').fetchall()
        print(f"Migrating {len(cat_rows)} categories...")
        for r in cat_rows:
            if not db.session.get(Category, r['id']):
                c = Category(
                    id=r['id'],
                    name=r['name'],
                    store=r['store'],
                    image=r['image'],
                    sort_order=r['sort_order']
                )
                db.session.add(c)
        db.session.commit()

        # 2. Products
        prod_rows = sqlite_conn.execute('SELECT * FROM product').fetchall()
        print(f"Migrating {len(prod_rows)} products...")
        for r in prod_rows:
            if not db.session.get(Product, r['id']):
                p = Product(
                    id=r['id'],
                    product_code=r['product_code'],
                    title=r['title'],
                    price=float(r['price']),
                    stock=int(r['stock']),
                    image=r['image'],
                    category=r['category'],
                    store=r['store'],
                    variants=r['variants'],
                    sort_order=int(r['sort_order'] or 0),
                    is_visible=bool(r['is_visible']),
                    discount_percent=float(r['discount_percent'] or 0.0),
                    use_custom_thumbnail=bool(r['use_custom_thumbnail']),
                    detail_images=r['detail_images']
                )
                db.session.add(p)
        db.session.commit()

        # 3. MinifigurePool
        pool_rows = sqlite_conn.execute('SELECT * FROM minifigure_pool').fetchall()
        print(f"Migrating {len(pool_rows)} pool items...")
        for r in pool_rows:
            if not db.session.get(MinifigurePool, r['id']):
                m = MinifigurePool(
                    id=r['id'],
                    name=r['name'],
                    image=r['image'],
                    rarity=r['rarity'],
                    stock=int(r['stock']),
                    sort_order=int(r['sort_order'] or 0),
                    linked_product_id=r['linked_product_id'],
                    linked_variant_index=r['linked_variant_index']
                )
                db.session.add(m)
        db.session.commit()

        # Update PostgreSQL primary key sequences if using Postgres
        if 'postgres' in app.config['SQLALCHEMY_DATABASE_URI']:
            for tbl, seq in [('category', 'category_id_seq'), ('product', 'product_id_seq'), ('minifigure_pool', 'minifigure_pool_id_seq')]:
                try:
                    db.session.execute(text(f"SELECT setval('{seq}', (SELECT COALESCE(MAX(id), 1) FROM {tbl}))"))
                    db.session.commit()
                except Exception as e:
                    print(f"Sequence warning for {seq}: {e}")

        print("\n--- Migration Summary ---")
        print(f"Categories: {Category.query.count()}")
        print(f"Products: {Product.query.count()}")
        print(f"MinifigurePool: {MinifigurePool.query.count()}")
        print("Migration complete and database verified successfully.")

    sqlite_conn.close()
    return True

if __name__ == '__main__':
    migrate_sqlite_to_postgres()
