from werkzeug.security import check_password_hash

def verify_captcha(user_input, stored_hash):
    return check_password_hash(stored_hash, user_input.upper())
