"""Email: IMAP/SMTP client, LiveKit tool wrapper and live new-mail listener."""

from .listener import start_email_listener
from .operations import EMAIL_TOOLS, email
from .tool import EmailConfig, EmailRecord, EmailTool

__all__ = [
    "EMAIL_TOOLS",
    "email",
    "EmailTool",
    "EmailConfig",
    "EmailRecord",
    "start_email_listener",
]
