import sys
import os

# Add store-admin to python path if needed
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from .admin_routes import admin_bp

__all__ = ['admin_bp']
