import uuid
import random
import string
from datetime import datetime, timedelta
from flask import jsonify
from werkzeug.security import generate_password_hash

def generate_captcha_controller(get_connection):
    captcha_text = ''.join(
        random.choices(string.ascii_uppercase + string.digits, k=6)
    )

    captcha_id = str(uuid.uuid4())
    captcha_hash = generate_password_hash(captcha_text)
    expires_at = datetime.utcnow() + timedelta(minutes=5)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO captcha_store (id, captcha_hash, expires_at)
        VALUES (%s, %s, %s)
    """, (captcha_id, captcha_hash, expires_at))

    conn.commit()
    cursor.close()
    conn.close()

    return jsonify({
        "status": "success",
        "captchaId": captcha_id,
        "captchaText": captcha_text   # 🔴 for dev only
    }), 200
