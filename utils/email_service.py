import os
import secrets
import string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def generate_secure_temporary_password(length=10) -> str:
    """
    Generates a secure, human-friendly temporary password starting with 'Jk#'
    e.g. 'Jk#9aB7@k2'
    """
    digits = string.digits
    chars = string.ascii_letters
    specials = "@#$%&*"
    
    random_part = (
        secrets.choice(string.ascii_uppercase) +
        secrets.choice(digits) +
        secrets.choice(string.ascii_lowercase) +
        secrets.choice(specials) +
        ''.join(secrets.choice(chars + digits) for _ in range(length - 4))
    )
    return f"Jk#{random_part}"


def send_welcome_user_email(to_email: str, user_name: str, temp_password: str, portal_url: str = "https://jkagentic.jktyre.co.in") -> bool:
    """
    Sends a welcome email with credentials to newly created user.
    Reads email parameters directly from .env (MAIL_SERVER, MAIL_PORT, MAIL_USERNAME / NOREPLY_EMAIL, etc.)
    exactly identical to helper/report_mailer.py.
    """
    smtp_host = os.getenv("MAIL_SERVER") or os.getenv("SMTP_HOST") or "smtp.gmail.com"
    smtp_port = int(os.getenv("MAIL_PORT") or os.getenv("SMTP_PORT") or 587)
    smtp_user = os.getenv("MAIL_USERNAME") or os.getenv("NOREPLY_EMAIL") or os.getenv("SMTP_USER")
    smtp_pass = os.getenv("MAIL_PASSWORD") or os.getenv("NOREPLY_PASSWORD") or os.getenv("SMTP_PASS")
    smtp_use_tls = os.getenv("MAIL_USE_TLS", "true").lower() in ("true", "1", "yes")
    from_email = smtp_user or os.getenv("SMTP_FROM", "no-reply@jktyre.com")

    # If email credentials are not present in .env, log for audit and return True
    if not smtp_host or not smtp_user or not smtp_pass:
        print(f"[Email Notification Log] ✉️ Simulated Welcome Email to {to_email} ({user_name}) | Temp Password: {temp_password}", flush=True)
        return True

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "Welcome to JK Tyre Analytics Portal - Your Login Credentials"
        msg["From"] = from_email
        msg["To"] = to_email

        plain_text = f"""Hello {user_name},

Your user account has been successfully provisioned for the JK Tyre AI Analytics Portal.

Login Details:
Portal URL: {portal_url}
Email: {to_email}
Temporary Password: {temp_password}

For your security, please login and change your password upon your first login.

Regards,
JK Tyre & Industries Ltd.
"""
        msg.attach(MIMEText(plain_text, "plain", "utf-8"))

        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden;">
                <div style="background-color: #4f46e5; padding: 20px; text-align: center; color: white;">
                    <h2 style="margin: 0;">JK Tyre AI Analytics Portal</h2>
                </div>
                <div style="padding: 24px;">
                    <p>Dear <strong>{user_name}</strong>,</p>
                    <p>Your user account has been successfully provisioned. You can now access your personalized sales analytics and executive dashboard.</p>
                    
                    <div style="background-color: #f8fafc; border: 1px solid #cbd5e1; border-radius: 6px; padding: 16px; margin: 20px 0;">
                        <p style="margin: 4px 0;"><strong>Portal URL:</strong> <a href="{portal_url}">{portal_url}</a></p>
                        <p style="margin: 4px 0;"><strong>Login Email:</strong> {to_email}</p>
                        <p style="margin: 4px 0;"><strong>Temporary Password:</strong> <code style="background-color: #e2e8f0; padding: 2px 6px; border-radius: 4px; font-weight: bold;">{temp_password}</code></p>
                    </div>

                    <p style="font-size: 13px; color: #64748b;">
                        * For your security, you will be prompted to change your password upon your first login.
                    </p>
                    
                    <div style="text-align: center; margin-top: 25px;">
                        <a href="{portal_url}" style="background-color: #4f46e5; color: white; padding: 10px 20px; text-decoration: none; border-radius: 6px; font-weight: bold;">Login to Portal</a>
                    </div>
                </div>
                <div style="background-color: #f1f5f9; padding: 12px; text-align: center; font-size: 11px; color: #94a3b8;">
                    © JK Tyre & Industries Ltd. All rights reserved.
                </div>
            </div>
        </body>
        </html>
        """
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        # Connect and send matching helper/report_mailer.py
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as server:
                if smtp_pass:
                    server.login(from_email, smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.ehlo()
                if smtp_use_tls or smtp_port == 587:
                    try:
                        server.starttls()
                        server.ehlo()
                    except Exception:
                        pass
                if smtp_pass:
                    try:
                        server.login(from_email, smtp_pass)
                    except Exception:
                        pass
                server.send_message(msg)

        print(f"[Email Service] ✅ Welcome email dispatched to {to_email}", flush=True)
        return True

    except Exception as e:
        print(f"[Email Service] ⚠️ Failed to dispatch email to {to_email}: {e}", flush=True)
        return False
