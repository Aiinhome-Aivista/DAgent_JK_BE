import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from database.config import NOREPLY_EMAIL, NOREPLY_PASSWORD

def send_otp_email(to_email, otp):
    try:
        # 1. Setup Email Headers
        msg = MIMEMultipart("alternative")
        msg["From"] = NOREPLY_EMAIL
        msg["To"] = to_email
        msg["Subject"] = "Security Alert: Your Password Reset Code"

        # 2. Plain Text Fallback
        text_body = f"""
        Hello,

        We received a request to reset the password for your SoulJunction account.

        Your Verification Code: {otp}

        This code is valid for 10 minutes. If you did not request this, please ignore this email.
        """

        # 3. Professional HTML Design
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
          <style>
            body {{ font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; background-color: #f8f9fa; margin: 0; padding: 0; }}
            .container {{ max-width: 600px; margin: 40px auto; background-color: #ffffff; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.05); overflow: hidden; }}
            .header {{ background-color: #2c3e50; padding: 30px; text-align: center; }}
            .header h1 {{ color: #ffffff; margin: 0; font-size: 24px; font-weight: 300; letter-spacing: 1px; }}
            .content {{ padding: 40px 30px; color: #555555; line-height: 1.6; text-align: center; }}
            .otp-box {{ background-color: #f1f4f6; border: 1px dashed #2c3e50; border-radius: 6px; padding: 20px; margin: 30px 0; display: inline-block; min-width: 200px; }}
            .otp-code {{ font-size: 36px; font-weight: 700; color: #2c3e50; letter-spacing: 8px; }}
            .warning {{ font-size: 13px; color: #e74c3c; margin-top: 20px; }}
            .footer {{ background-color: #f8f9fa; padding: 20px; text-align: center; font-size: 12px; color: #95a5a6; border-top: 1px solid #eeeeee; }}
            .button {{ display: inline-block; padding: 12px 24px; background-color: #27ae60; color: #ffffff; text-decoration: none; border-radius: 5px; font-weight: bold; margin-top: 20px; }}
          </style>
        </head>
        <body>
          <div class="container">
            
            <div class="header">
              <h1>SOULJUNCTION</h1>
            </div>

            <div class="content">
              <p style="font-size: 18px; color: #333;">Password Reset Request</p>
              <p>We received a request to reset your password. Use the code below to proceed.</p>

              <div class="otp-box">
                <div class="otp-code">{otp}</div>
              </div>

              <p>This code will expire in <strong>10 minutes</strong>.</p>
              
              <div class="warning">
                If you did not request a password reset, you can safely ignore this email. Your account remains secure.
              </div>
            </div>

            <div class="footer">
              <p>&copy; 2026 Soul Junction. All rights reserved.</p>
              <p>This is an automated message, please do not reply.</p>
            </div>
          </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        # 4. Send using Namecheap (SSL Port 465)
        with smtplib.SMTP_SSL("mail.souljunction.life", 465) as server:
            server.login(NOREPLY_EMAIL, NOREPLY_PASSWORD)
            server.send_message(msg)

        return True

    except Exception as e:
        print(f"OTP Email Error: {e}")
        return False