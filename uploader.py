import os
import uuid
from werkzeug.utils import secure_filename
import cloudinary
import cloudinary.uploader

APP_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(APP_DIR, 'static', 'uploads')

CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "dwwearehy").strip()
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "998136922585551").strip()
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "mL1MT4pxmbzUiWsW9BagJNtlhh8").strip()

if CLOUDINARY_API_KEY and CLOUDINARY_API_KEY != "dummy_key":
    try:
        cloudinary.config(
            cloud_name=CLOUDINARY_CLOUD_NAME,
            api_key=CLOUDINARY_API_KEY,
            api_secret=CLOUDINARY_API_SECRET,
            secure=True
        )
    except Exception as e:
        print(f"[Upload] Cloudinary configuration warning: {e}")

def optimize_and_upload(file):
    """
    Uploads an image file safely:
    1. If real Cloudinary credentials are provided, attempts Cloudinary upload.
    2. If Cloudinary credentials are missing/dummy or if upload fails (e.g. localhost/offline),
       gracefully saves the file to static/uploads/ and returns {'secure_url': '/static/uploads/<filename>'}.
    """
    if not file:
        return {'secure_url': '/static/images/hero-bg.jpg'}

    if isinstance(file, str):
        return {'secure_url': file}

    # Attempt Cloudinary if real credentials exist
    if CLOUDINARY_API_KEY and CLOUDINARY_API_KEY != "dummy_key" and CLOUDINARY_API_SECRET and CLOUDINARY_API_SECRET != "dummy_secret":
        try:
            return cloudinary.uploader.upload(
                file, format="webp", quality="auto", width=900, height=900, crop="limit"
            )
        except Exception as e:
            print(f"[Upload] Cloudinary upload failed ({e}); falling back to local file storage.")

    # Local file storage fallback
    try:
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        orig_filename = getattr(file, 'filename', '') or 'upload.jpg'
        clean_name = secure_filename(orig_filename) or 'image.jpg'
        unique_name = f"{uuid.uuid4().hex[:10]}_{clean_name}"
        save_path = os.path.join(UPLOAD_FOLDER, unique_name)

        if hasattr(file, 'seek'):
            try:
                file.seek(0)
            except Exception:
                pass

        file.save(save_path)
        return {'secure_url': f'/static/uploads/{unique_name}'}
    except Exception as e:
        print(f"[Upload] Local save error: {e}")
        return {'secure_url': '/static/images/hero-bg.jpg'}
