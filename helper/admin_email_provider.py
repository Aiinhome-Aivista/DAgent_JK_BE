import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from database.config import NOREPLY_EMAIL, NOREPLY_PASSWORD, ADMIN_EMAIL


def send_admin_payment_email(user_email, amount, payment_id, plan_name):
    try:
        # Normalize admin emails
        if isinstance(ADMIN_EMAIL, str):
            admin_list = [e.strip() for e in ADMIN_EMAIL.split(",")]
        else:
            admin_list = ADMIN_EMAIL

        msg = MIMEMultipart()
        msg["From"] = NOREPLY_EMAIL
        msg["To"] = ", ".join(admin_list)
        msg["Subject"] = "💰 New Payment Received - SoulJunction"

        body = f"""
New payment received successfully.

User Email: {user_email}
Plan: {plan_name}
Amount: USD {amount}
Transaction ID: {payment_id}
Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}


        """

        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("mail.souljunction.life", 465, timeout=20) as server:
            server.login(NOREPLY_EMAIL, NOREPLY_PASSWORD)
            server.sendmail(
                NOREPLY_EMAIL,
                admin_list,
                msg.as_string()
            )

        print("✅ Admin email sent to:", admin_list)
        return True

    except Exception as e:
        print("❌ Admin Email Error:", repr(e))
        return False
