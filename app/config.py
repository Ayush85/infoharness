"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v not in (None, "") else default


def _float(name: str, default: float) -> float:
    v = os.getenv(name)
    return float(v) if v not in (None, "") else default


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./infoharness.db"))
    public_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"))

    # Crawl limits
    max_pages: int = field(default_factory=lambda: _int("MAX_PAGES", 300))
    max_chunks: int = field(default_factory=lambda: _int("MAX_CHUNKS", 4000))
    crawl_concurrency: int = field(default_factory=lambda: _int("CRAWL_CONCURRENCY", 5))
    crawl_timeout: float = field(default_factory=lambda: _float("CRAWL_TIMEOUT", 15.0))
    max_page_bytes: int = field(default_factory=lambda: _int("MAX_PAGE_BYTES", 2_000_000))
    max_scan_seconds: int = field(default_factory=lambda: _int("MAX_SCAN_SECONDS", 600))
    user_agent: str = field(default_factory=lambda: os.getenv(
        "USER_AGENT", "InfoharnessBot/0.1 (+https://infoharness.com/bot)"))
    allow_private_hosts: bool = field(default_factory=lambda: _bool("ALLOW_PRIVATE_HOSTS", False))

    # Link checking
    check_external_links: bool = field(default_factory=lambda: _bool("CHECK_EXTERNAL_LINKS", True))
    max_link_checks: int = field(default_factory=lambda: _int("MAX_LINK_CHECKS", 400))
    pro_max_link_checks: int = field(default_factory=lambda: _int("PRO_MAX_LINK_CHECKS", 3000))
    link_check_seconds: int = field(default_factory=lambda: _int("LINK_CHECK_SECONDS", 240))

    # Pro plan scan size (free scans use MAX_PAGES / MAX_SCAN_SECONDS)
    pro_max_pages: int = field(default_factory=lambda: _int("PRO_MAX_PAGES", 2000))
    pro_scan_seconds: int = field(default_factory=lambda: _int("PRO_SCAN_SECONDS", 1500))

    # Abuse protection
    scans_per_ip_per_day: int = field(default_factory=lambda: _int("SCANS_PER_IP_PER_DAY", 5))
    scans_per_user_per_day: int = field(default_factory=lambda: _int("SCANS_PER_USER_PER_DAY", 20))
    max_queue: int = field(default_factory=lambda: _int("MAX_QUEUE", 50))
    cache_hours: int = field(default_factory=lambda: _int("CACHE_HOURS", 24))
    trust_proxy: bool = field(default_factory=lambda: _bool("TRUST_PROXY", False))
    ip_hash_salt: str = field(default_factory=lambda: os.getenv("IP_HASH_SALT", "change-me"))

    # Embeddings: "tfidf" (free, offline) or "openai" (any OpenAI-compatible /embeddings endpoint)
    embeddings_provider: str = field(default_factory=lambda: os.getenv("EMBEDDINGS_PROVIDER", "tfidf").lower())
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"))
    embeddings_model: str = field(default_factory=lambda: os.getenv("EMBEDDINGS_MODEL", "text-embedding-3-small"))

    # LLM judge: "none", "anthropic", or "openai" (OpenAI-compatible chat completions)
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "none").lower())
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "claude-haiku-5-5"))
    llm_max_pairs: int = field(default_factory=lambda: _int("LLM_MAX_PAIRS", 60))
    llm_min_confidence: float = field(default_factory=lambda: _float("LLM_MIN_CONFIDENCE", 0.7))

    # Accounts, email, plans
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", "") or os.getenv("IP_HASH_SALT", "change-me"))
    mail_from: str = field(default_factory=lambda: os.getenv("MAIL_FROM", "infoharness <hello@infoharness.com>"))
    resend_api_key: str = field(default_factory=lambda: os.getenv("RESEND_API_KEY", ""))
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    admin_emails: str = field(default_factory=lambda: os.getenv("ADMIN_EMAILS", ""))  # comma separated; always Pro
    free_sites: int = field(default_factory=lambda: _int("FREE_SITES", 1))
    pro_sites: int = field(default_factory=lambda: _int("PRO_SITES", 5))
    checkout_url: str = field(default_factory=lambda: os.getenv("CHECKOUT_URL", ""))  # hosted checkout (Lemon Squeezy / Paddle)
    ls_webhook_secret: str = field(default_factory=lambda: os.getenv("LS_WEBHOOK_SECRET", ""))
    pro_price: str = field(default_factory=lambda: os.getenv("PRO_PRICE", "$19"))

    # Worker
    worker_poll_seconds: float = field(default_factory=lambda: _float("WORKER_POLL_SECONDS", 2.0))

    @property
    def admins(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def mail_enabled(self) -> bool:
        return bool(self.resend_api_key or self.smtp_host)

    @property
    def llm_enabled(self) -> bool:
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        return False


settings = Settings()


def reload_settings() -> Settings:
    """Re-read env vars (used by tests)."""
    global settings
    settings = Settings()
    return settings
