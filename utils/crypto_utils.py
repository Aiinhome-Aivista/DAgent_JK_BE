from cryptography.fernet import Fernet
import base64
import os

# Hardcoded 32-byte key for simplicity. In production this should be an env variable.
SECRET_KEY = b'MySuperSecretKey1234567890123456'
fernet = Fernet(base64.urlsafe_b64encode(SECRET_KEY))

def encrypt_password(password: str) -> str:
    if not password:
        return password
    return fernet.encrypt(password.encode()).decode()

def decrypt_password(encrypted_password: str) -> str:
    if not encrypted_password:
        return encrypted_password
    try:
        return fernet.decrypt(encrypted_password.encode()).decode()
    except Exception:
        # Fallback for plain text passwords in the DB
        return encrypted_password
