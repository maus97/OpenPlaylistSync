"""Small SQLAlchemy repositories used by application services."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ops.models import (
    AppConfiguration,
    ProviderAccount,
    SyncAction,
    SyncBaseline,
    SyncPair,
    SyncRun,
)
from ops.security.crypto import CredentialCipher


class ProviderAccountRepository:
    """Persistence operations for provider accounts and encrypted credentials."""

    def __init__(self, session: Session, cipher: CredentialCipher) -> None:
        self.session = session
        self.cipher = cipher

    def get(self, account_id: int) -> ProviderAccount | None:
        return self.session.get(ProviderAccount, account_id)

    def get_by_external_id(
        self, provider_name: str, external_account_id: str
    ) -> ProviderAccount | None:
        statement = select(ProviderAccount).where(
            ProviderAccount.provider_name == provider_name,
            ProviderAccount.external_account_id == external_account_id,
        )
        return self.session.scalar(statement)

    def save_credentials(
        self,
        account: ProviderAccount,
        credentials: Mapping[str, Any],
        *,
        key_id: str = "primary",
    ) -> ProviderAccount:
        account.credentials_ciphertext = self.cipher.encrypt(credentials)
        account.credential_key_id = key_id
        self.session.add(account)
        self.session.flush()
        return account

    def load_credentials(self, account: ProviderAccount) -> dict[str, Any]:
        if not account.credentials_ciphertext:
            return {}
        return self.cipher.decrypt(account.credentials_ciphertext)


class AppConfigurationRepository:
    """Persist encrypted application settings entered by the operator."""

    CONFIGURATION_ID = 1

    def __init__(self, session: Session, cipher: CredentialCipher) -> None:
        self.session = session
        self.cipher = cipher

    def load(self) -> dict[str, Any]:
        configuration = self.session.get(AppConfiguration, self.CONFIGURATION_ID)
        if configuration is None:
            return {}
        return self.cipher.decrypt(configuration.settings_ciphertext)

    def save(self, values: Mapping[str, Any]) -> AppConfiguration:
        configuration = self.session.get(AppConfiguration, self.CONFIGURATION_ID)
        if configuration is None:
            configuration = AppConfiguration(id=self.CONFIGURATION_ID, settings_ciphertext="")
        configuration.settings_ciphertext = self.cipher.encrypt(values)
        configuration.credential_key_id = "primary"
        self.session.add(configuration)
        self.session.flush()
        return configuration


class SyncPairRepository:
    """Persistence operations for configured playlist pairs."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_enabled(self) -> list[SyncPair]:
        statement = select(SyncPair).where(SyncPair.enabled.is_(True)).order_by(SyncPair.id)
        return list(self.session.scalars(statement))

    def get(self, pair_id: int) -> SyncPair | None:
        return self.session.get(SyncPair, pair_id)

    def all(self) -> list[SyncPair]:
        statement = select(SyncPair).order_by(SyncPair.id)
        return list(self.session.scalars(statement))

    def save(self, pair: SyncPair) -> SyncPair:
        self.session.add(pair)
        self.session.flush()
        return pair


class SyncBaselineRepository:
    """Persistence operations for last-successful baselines."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def latest_for_pair(self, pair_id: int) -> SyncBaseline | None:
        statement = (
            select(SyncBaseline)
            .where(SyncBaseline.pair_id == pair_id)
            .order_by(SyncBaseline.synchronized_at.desc())
            .limit(1)
        )
        return self.session.scalar(statement)

    def save(self, baseline: SyncBaseline) -> SyncBaseline:
        self.session.add(baseline)
        self.session.flush()
        return baseline


class SyncRunRepository:
    """Persistence operations for audit-friendly sync attempts."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def start(
        self,
        baseline_id: int | None = None,
        *,
        pair_id: int | None = None,
        fingerprint: str | None = None,
    ) -> SyncRun:
        run = SyncRun(
            baseline_id=baseline_id,
            pair_id=pair_id,
            plan_fingerprint=fingerprint,
            status="running",
        )
        self.session.add(run)
        self.session.flush()
        return run

    def recent(self, limit: int = 20) -> list[SyncRun]:
        statement = (
            select(SyncRun).order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(limit)
        )
        return list(self.session.scalars(statement))

    def finish(self, run: SyncRun, status: str, summary_json: str | None = None) -> SyncRun:
        run.status = status
        run.summary_json = summary_json
        run.completed_at = datetime.now().astimezone()
        self.session.add(run)
        self.session.flush()
        return run


class SyncActionRepository:
    """Store per-action execution progress for inspection and recovery."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def plan(
        self, run: SyncRun, ordinal: int, provider_name: str, operation: str, track_key: str
    ) -> SyncAction:
        action = SyncAction(
            run_id=run.id,
            ordinal=ordinal,
            provider_name=provider_name,
            operation=operation,
            track_key=track_key,
            status="planned",
        )
        self.session.add(action)
        self.session.flush()
        return action

    def complete(self, action: SyncAction) -> SyncAction:
        action.status = "completed"
        action.completed_at = datetime.now().astimezone()
        self.session.add(action)
        self.session.flush()
        return action

    def fail(self, action: SyncAction, error: str) -> SyncAction:
        action.status = "failed"
        action.error_summary = error[:500]
        action.completed_at = datetime.now().astimezone()
        self.session.add(action)
        self.session.flush()
        return action

    def for_run(self, run_id: int) -> list[SyncAction]:
        statement = (
            select(SyncAction).where(SyncAction.run_id == run_id).order_by(SyncAction.ordinal)
        )
        return list(self.session.scalars(statement))
