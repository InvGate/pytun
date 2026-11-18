import enum
import smtplib
import ssl
from email.mime.text import MIMEText
from urllib.parse import urlsplit

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


OAUTH_KEYS = ("token_url", "client_id", "client_secret", "scope")


class OAuthTokenError(Exception):
    """The OAuth token request failed. The message says why, and never carries the client secret."""


def fetch_oauth_token(token_url, client_id, client_secret, scope):
    """OAuth2 client credentials grant (RFC 6749 section 4.4).

    No token cache: alerts are rate limited per tunnel, so a fresh token per
    email costs one extra HTTPS request at most every few minutes.
    """
    try:
        response = requests.post(token_url, data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": scope,
        }, timeout=SMTP_CONNECTION_TIMEOUT)
    except requests.exceptions.SSLError as e:
        raise OAuthTokenError(
            "OAuth token request failed, the token endpoint certificate is not trusted: %s. This request uses the "
            "CA bundle shipped with the Connector, not the system store, and smtp_tls_verify does not apply to it: "
            "exclude the token URL from TLS inspection" % (e,)) from e
    except requests.RequestException as e:
        raise OAuthTokenError("OAuth token request failed: %s" % (e,)) from e

    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    if response.ok and body.get("access_token"):
        return body["access_token"]
    if "error" in body:  # RFC 6749 section 5.2; Entra puts its AADSTS code in error_description
        reason = "%s: %s" % (body["error"], body.get("error_description", ""))
    else:
        # Never the raw body: a wrong URL can echo the request back, client_secret included.
        reason = "no access_token in the response (Content-Type: %s)" % (response.headers.get("Content-Type"),)
    raise OAuthTokenError("OAuth token request failed (%s): %s" % (response.status_code, reason))


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
            missing = ["smtp_oauth_" + key for key in OAUTH_KEYS if not oauth.get(key)]
            if missing:
                raise ValueError("OAuth needs all four smtp_oauth_* keys, missing: %s" % ", ".join(missing))
            if urlsplit(oauth["token_url"]).scheme != "https":
                raise ValueError("smtp_oauth_token_url must use https: the client secret is sent to it")
            if not login:
                raise ValueError("smtp_login is required with OAuth: it is the mailbox the alerts are sent as")
            if not login.isascii():
                raise ValueError("smtp_login must be ASCII with OAuth: smtplib sends the XOAUTH2 string as ASCII")
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
            # None is smtplib's own default, which does not verify the server certificate. Built per send, not
            # cached, so a CA installed after startup (the fix for an untrusted certificate) needs no restart.
            context = None
            if self.tls_verify and self.security != SecurityValues.none:
                context = ssl.create_default_context()

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
                self.logger.error("The SMTP server certificate is not trusted (SMTP connection, not the OAuth token "
                                  "request): install its CA in the system certificate store, or set "
                                  "smtp_tls_verify = False (not recommended)")
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
