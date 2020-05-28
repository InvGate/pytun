import smtplib
from email.mime.text import MIMEText

from email_validator import validate_email

from alerts.alert_sender import AlertSender


class EmailAlertSender(AlertSender):

    def __init__(self, host, login, password, to_address, logger, security=None, port=25, from_address = None):
        if from_address is None:
            from_address = login
        if security is not None:
            if security not in ("none", "tls", "ssl"):
                raise ValueError("Security can only be none, tls or ssl but %s was received", security)
        else:
            security = None
        self.security = security
        self.port = port
        self.host = host
        self.login = login
        self.password = password
        self.sender_email = validate_email(from_address).email
        self.receiver_email = validate_email(to_address).email
        self.logger = logger

    def send_alert(self, tunnel_name):
        try:
            message = MIMEText("This email is to let you know that Tunnel %s is down!"%tunnel_name, 'plain')
            message["Subject"] = "Tunnel %s is down!"%tunnel_name
            message["From"] = self.sender_email
            message["To"] = self.receiver_email
            smtp_class = smtplib.SMTP_SSL if self.security == 'ssl' else smtplib.SMTP
            with smtp_class(self.host, self.port) as server:
                if self.security == 'tls':
                    server.starttls()
                server.login(self.login, self.password)
                res = server.sendmail(
                    self.sender_email, self.receiver_email, message.as_string()
                )
                if res:
                    self.logger.warning("It was not possible to send email: %s", res)
        except Exception as e:
            self.logger.exception("Failed to send email")