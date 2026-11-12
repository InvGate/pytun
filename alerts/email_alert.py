import enum
import smtplib
import ssl
from email.mime.text import MIMEText

import requests
from email_validator import validate_email

from alerts.alert_sender import AlertSender
from lib import ratelimit_by_args

SMTP_CONNECTION_TIMEOUT = 10
SMTP_ALERT_RATE_LIMIT = 600  # 600 seconds = 10 min


class SecurityValues(enum.Enum):
    none = "none"
    tls = "tls"
    ssl = "ssl"


def fetch_oauth_token(token_url, client_id, client_secret, scope):
    """OAuth2 client credentials grant (RFC 6749 section 4.4).

    No token cache: alerts are rate limited per tunnel, so a fresh token per
    email costs one extra HTTPS request at most every few minutes.
    """
    response = requests.post(token_url, data={
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope,
    }, timeout=SMTP_CONNECTION_TIMEOUT)
    if not response.ok:
        # The body carries the provider's reason (e.g. Entra's AADSTS code), never the secret.
        raise requests.HTTPError("OAuth token request failed (%s): %s" % (response.status_code, response.text),
                                 response=response)
    return response.json()["access_token"]


class EmailAlertSender(AlertSender):

    def __init__(self, tunnel_manager_id, host, login, password, to_address, logger, security=None, port=25,
                 from_address=None, oauth=None, tls_verify=True):
        """oauth: None, or the fetch_oauth_token() keyword arguments to authenticate with XOAUTH2.
        tls_verify: verify the server certificate and hostname on tls/ssl connections.
        """
        self.tunnel_manager_id = tunnel_manager_id
        if from_address is None:
            from_address = login
        auth = "oauth2" if oauth else "password" if login else "none"
        logger.info("Creating email sender: host=%s port=%s security=%s auth=%s login=%s from=%s to=%s",
                    host, port, security, auth, login, from_address, to_address)
        if security is not None:
            if security not in SecurityValues.__members__:
                raise ValueError("Security can only be none, tls or ssl but '%s' was received" % (security,))
            else:
                security = SecurityValues[security]
        else:
            security = SecurityValues.none

        if oauth:
            if not login:
                raise ValueError("smtp_login is required with OAuth: it is the mailbox the alerts are sent as")
            if security == SecurityValues.none:
                raise ValueError("OAuth requires smtp_security tls or ssl: the access token must not travel "
                                 "in clear text")

        if not tls_verify and security != SecurityValues.none:
            logger.warning("SMTP TLS certificate verification is disabled (smtp_tls_verify = False): "
                           "the SMTP credentials are exposed to anyone who can intercept the connection")

        self.oauth = oauth
        self.tls_verify = tls_verify
        self.security = security
        self.port = port
        self.host = host
        self.login = login
        self.password = password
        self.sender_email = validate_email(from_address, check_deliverability=False).normalized
        self.receiver_email = validate_email(to_address, check_deliverability=False).normalized
        self.logger = logger

    @ratelimit_by_args(calls=1, period=SMTP_ALERT_RATE_LIMIT)
    def send_alert(self, tunnel_name, message=None, exception_on_failure=False):
        try:
            message = self._build_message(tunnel_name, message)
            token = fetch_oauth_token(**self.oauth) if self.oauth else None
            # None is smtplib's own default, which does not verify the server certificate.
            context = ssl.create_default_context() if self.tls_verify else None

            if self.security == SecurityValues.ssl:
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=SMTP_CONNECTION_TIMEOUT, context=context)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=SMTP_CONNECTION_TIMEOUT)

            with server:
                if self.security == SecurityValues.tls:
                    server.starttls(context=context)
                if self.oauth:
                    server.ehlo_or_helo_if_needed()  # login() does this itself, auth() does not
                    xoauth2 = "user=%s\x01auth=Bearer %s\x01\x01" % (self.login, token)
                    # A rejected token comes back as an error challenge; answering it empty gets the final 535.
                    server.auth("XOAUTH2", lambda challenge=None: xoauth2 if challenge is None else "")
                elif self.login:
                    server.login(self.login, self.password)
                res = server.sendmail(
                    self.sender_email, self.receiver_email, message.as_string()
                )
                if res:
                    self.logger.warning("It was not possible to send email: %s", res)
        except Exception as e:
            self.logger.exception("Failed to send email %s" % (e,))
            if isinstance(e, ssl.SSLCertVerificationError):
                self.logger.error("The SMTP server certificate is not trusted: install its CA in the system "
                                  "certificate store, or set smtp_tls_verify = False (not recommended)")
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
