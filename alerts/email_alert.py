import enum
import smtplib
from email.mime.text import MIMEText

from email_validator import validate_email

from alerts.alert_sender import AlertSender
from lib import ratelimit_by_args

SMTP_CONNECTION_TIMEOUT = 10
SMTP_ALERT_RATE_LIMIT = 600  # 600 seconds = 10 min


class SecurityValues(enum.Enum):
    none = "none"
    tls = "tls"
    ssl = "ssl"


class EmailAlertSender(AlertSender):

    def __init__(self, tunnel_manager_id, host, login, password, to_address, logger, security=None, port=25,
                 from_address=None):
        self.tunnel_manager_id = tunnel_manager_id
        logger.info("Creating email sender with parameters" + str(
            (tunnel_manager_id, host, login, password, to_address, security, port, from_address)))
        if from_address is None:
            from_address = login
        if security is not None:
            if security not in SecurityValues.__members__:
                raise ValueError("Security can only be none, tls or ssl but '%s' was received" % (security,))
            else:
                security = SecurityValues[security]
        else:
            security = SecurityValues.none

        self.security = security
        self.port = port
        self.host = host
        self.login = login
        self.password = password
        self.sender_email = validate_email(from_address).email
        self.receiver_email = validate_email(to_address).email
        self.logger = logger

    @ratelimit_by_args(calls=1, period=SMTP_ALERT_RATE_LIMIT)
    def send_alert(self, tunnel_name, message=None, exception_on_failure=False):
        try:
            message = self._build_message(tunnel_name, message)
            smtp_class = smtplib.SMTP_SSL if self.security == SecurityValues.ssl else smtplib.SMTP

            with smtp_class(self.host, self.port, timeout=SMTP_CONNECTION_TIMEOUT) as server:
                if self.security == SecurityValues.tls:
                    server.starttls()
                if self.login:
                    server.login(self.login, self.password)
                res = server.sendmail(
                    self.sender_email, self.receiver_email, message.as_string()
                )
                if res:
                    self.logger.warning("It was not possible to send email: %s", res)
        except Exception as e:
            self.logger.exception("Failed to send email %s" % (e,))
            if exception_on_failure:
                raise e

    def _build_message(self, tunnel_name, message_text):
        if not tunnel_name and not message_text:
            raise ValueError("Can't build message without information about it. "
                             "tunnel_name or message_text are required.")

        if message_text:
            message = MIMEText(message_text, 'plain')
        else:
            message = MIMEText(
                f"This email is to let you know that {tunnel_name} is down! Manager id: {self.tunnel_manager_id}",
                'plain'
            )

        message["Subject"] = f"Connector {tunnel_name} notification" if tunnel_name else "Connector notification"
        message["From"] = self.sender_email
        message["To"] = self.receiver_email
        return message
