"""Initial persistence boundary for accounts, baselines, and run records."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from ops.db import Base


class ProviderAccount(Base):
    """A provider account with ciphertext-only credential storage."""

    __tablename__ = "provider_accounts"
    __table_args__ = (UniqueConstraint("provider_name", "external_account_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_lock_token: Mapped[str | None] = mapped_column(String(128))
    refresh_lock_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    external_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    credentials_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_key_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProviderIncident(Base):
    """Safe failure evidence; resolved incidents remain as history."""

    __tablename__ = "provider_incidents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    pair_id: Mapped[int | None] = mapped_column(ForeignKey("sync_pairs.id"), index=True)
    category: Mapped[str] = mapped_column(String(32))
    operation: Mapped[str] = mapped_column(String(64))
    resource: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    retry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppConfiguration(Base):
    """Encrypted operator configuration managed through the local UI."""

    __tablename__ = "app_configuration"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settings_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    credential_key_id: Mapped[str] = mapped_column(String(128), nullable=False, default="primary")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LocalAdministrator(Base):
    """The single local OPS operator credential, stored only as a password verifier."""

    __tablename__ = "local_administrator"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    session_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LoginRateLimit(Base):
    """Short-lived, privacy-preserving rate-limit state keyed by a hashed client address."""

    __tablename__ = "login_rate_limits"

    source_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SyncPair(Base):
    """The two provider accounts and playlist IDs participating in a sync."""

    __tablename__ = "sync_pairs"
    __table_args__ = (
        UniqueConstraint(
            "source_account_id",
            "target_account_id",
            "source_playlist_id",
            "target_playlist_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_account_id: Mapped[int] = mapped_column(
        ForeignKey("provider_accounts.id"), nullable=False
    )
    target_account_id: Mapped[int] = mapped_column(
        ForeignKey("provider_accounts.id"), nullable=False
    )
    source_playlist_id: Mapped[str] = mapped_column(String(255), nullable=False)
    target_playlist_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sync_mode: Mapped[str] = mapped_column(String(32), default="two_way", nullable=False)
    initial_sync_policy: Mapped[str] = mapped_column(String(32), default="merge", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    operation_lock_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation_lock_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    automatic_lock_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    automatic_lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    automatic_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    automatic_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    automatic_succeeded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    automatic_next_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    automatic_outcome: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProviderTrackMapping(Base):
    """A verified provider track ID mapped to OPS's canonical sync key."""

    __tablename__ = "provider_track_mappings"
    __table_args__ = (UniqueConstraint("pair_id", "account_id", "provider_track_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pair_id: Mapped[int] = mapped_column(ForeignKey("sync_pairs.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("provider_accounts.id"), nullable=False)
    provider_track_id: Mapped[str] = mapped_column(String(255), nullable=False)
    canonical_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_provider_track_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_isrc: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance: Mapped[str] = mapped_column(String(32), nullable=False, default="successful_add")
    identity_version: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ProviderSearchCache(Base):
    """Bounded reusable search evidence that reduces provider catalogue calls."""

    __tablename__ = "provider_search_cache"
    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "provider_name",
            "track_fingerprint",
            "algorithm_version",
            name="uq_provider_search_cache_identity",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("provider_accounts.id"), nullable=False)
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    track_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_track_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_tracks_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SyncBaseline(Base):
    """The last successful normalized snapshot for a synchronization pair."""

    __tablename__ = "sync_baselines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pair_id: Mapped[int | None] = mapped_column(ForeignKey("sync_pairs.id"), nullable=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("provider_accounts.id"), nullable=False)
    playlist_key: Mapped[str] = mapped_column(String(255), nullable=False)
    source_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    target_provider: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    identity_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    synchronized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SyncRun(Base):
    """An attempt to plan or apply synchronization changes."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    baseline_id: Mapped[int | None] = mapped_column(ForeignKey("sync_baselines.id"), nullable=True)
    pair_id: Mapped[int | None] = mapped_column(ForeignKey("sync_pairs.id"), nullable=True)
    plan_fingerprint: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidate_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_state_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target_state_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approval_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approval_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approval_consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SyncAction(Base):
    """Durable, redacted journal entry for one provider write."""

    __tablename__ = "sync_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("sync_runs.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_name: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    track_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="planned")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
