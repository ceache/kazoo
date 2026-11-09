"""SASL Configuration and Sans-I/O Handshake Stepper for Kazoo."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
import warnings

from kazoo.exceptions import (
    AuthFailedError,
    ConfigurationError,
    SASLException,
)

try:
    import puresasl  # type: ignore[import-untyped,import-not-found]
    import puresasl.client  # type: ignore[import-untyped,import-not-found]

    PURESASL_AVAILABLE = True
except ImportError:
    PURESASL_AVAILABLE = False


class SASLClientProtocol(Protocol):
    """Structural protocol for puresasl.client.SASLClient."""

    @property
    def complete(self) -> bool: ...

    def process(self, challenge: bytes | None = None) -> bytes | None: ...

    def dispose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SASLConfig:
    """Immutable SASL configuration normalized from client options."""

    mechanism: str
    service: str = "zookeeper"
    username: str | None = None
    password: str | None = None
    principal: str | None = None
    extra_options: tuple[tuple[str, Any], ...] = ()

    def effective_host(self, host: str) -> str:
        """Return the SASL host parameter expected by ZooKeeper.

        ZooKeeper hardcodes the DIGEST-MD5 domain to ``zk-sasl-md5`` instead
        of using the server hostname (see SecurityUtils.java).
        """
        if self.mechanism == "DIGEST-MD5":
            return "zk-sasl-md5"
        return host

    def to_dict(self) -> dict[str, Any]:
        """Convert configuration back into a dictionary for puresasl."""
        opts: dict[str, Any] = dict(self.extra_options)
        opts["mechanism"] = self.mechanism
        opts["service"] = self.service
        if self.username is not None:
            opts["username"] = self.username
        if self.password is not None:
            opts["password"] = self.password
        if self.principal is not None:
            opts["principal"] = self.principal
        return opts

    def to_client_kwargs(self, host: str) -> dict[str, Any]:
        """Build keyword arguments for ``puresasl.client.SASLClient``."""
        kwargs = self.to_dict()
        kwargs["host"] = self.effective_host(host)
        return kwargs

    @classmethod
    def from_dict(cls, options: Mapping[str, Any]) -> SASLConfig:
        """Construct a :class:`SASLConfig` from a raw options mapping."""
        if "mechanism" not in options:
            raise ConfigurationError("SASL options must specify a mechanism")
        mechanism = str(options["mechanism"]).upper()
        if mechanism not in ("DIGEST-MD5", "GSSAPI", "PLAIN"):
            raise ConfigurationError(
                "SASL mechanism must be DIGEST-MD5, GSSAPI, or PLAIN, "
                f"got {mechanism}"
            )
        if mechanism in ("DIGEST-MD5", "PLAIN") and not options.get(
            "username"
        ):
            raise ConfigurationError(
                f"SASL mechanism {mechanism} requires a username"
            )
        if mechanism in ("DIGEST-MD5", "PLAIN") and not options.get(
            "password"
        ):
            raise ConfigurationError(
                f"SASL mechanism {mechanism} requires a password"
            )
        known_keys = {
            "mechanism",
            "service",
            "username",
            "password",
            "principal",
        }
        extra = tuple(
            (k, v) for k, v in options.items() if k not in known_keys
        )
        return cls(
            mechanism=mechanism,
            service=str(options.get("service", "zookeeper")),
            username=options.get("username"),
            password=options.get("password"),
            principal=options.get("principal"),
            extra_options=extra,
        )

    @classmethod
    def from_client_options(
        cls,
        sasl_options: Mapping[str, Any] | SASLConfig | None,
        auth_data: Iterable[tuple[str, str]] = (),
    ) -> tuple[SASLConfig | None, set[tuple[str, str]]]:
        """Normalize ``sasl_options`` and legacy ``auth_data`` SASL entries."""
        legacy_config: SASLConfig | None = None
        filtered_auth: set[tuple[str, str]] = set()

        for scheme, auth in auth_data:
            if scheme != "sasl":
                filtered_auth.add((scheme, auth))
                continue
            if sasl_options or legacy_config is not None:
                raise ConfigurationError(
                    "Multiple SASL configurations provided"
                )
            warnings.warn(
                "Passing SASL configuration as part of the auth_data is "
                "deprecated, please use the sasl_options configuration "
                "instead",
                DeprecationWarning,
                stacklevel=3,
            )
            username, password = auth.split(":")
            legacy_config = cls(
                mechanism="DIGEST-MD5",
                service="zookeeper",
                username=username,
                password=password,
                principal="zk-sasl-md5",
            )

        if legacy_config is not None:
            return legacy_config, filtered_auth
        if sasl_options is None:
            return None, filtered_auth
        if isinstance(sasl_options, SASLConfig):
            return sasl_options, filtered_auth
        if not sasl_options:
            return None, filtered_auth
        return cls.from_dict(sasl_options), filtered_auth


class SASLExchange:
    """Sans-I/O driver for a single SASL challenge-response exchange."""

    def __init__(
        self,
        config: SASLConfig | Mapping[str, Any],
        host: str,
        client_factory: Callable[..., SASLClientProtocol] | None = None,
    ) -> None:
        sasl_cfg = (
            config
            if isinstance(config, SASLConfig)
            else SASLConfig.from_dict(config)
        )
        self.config = sasl_cfg
        kwargs = sasl_cfg.to_client_kwargs(host)
        if client_factory is not None:
            self._sasl_cli: SASLClientProtocol = client_factory(**kwargs)
        else:
            if not PURESASL_AVAILABLE:
                raise SASLException("Missing SASL support")
            self._sasl_cli = puresasl.client.SASLClient(  # type: ignore[no-untyped-call]
                **kwargs
            )

    @property
    def client(self) -> SASLClientProtocol:
        """Underlying puresasl SASLClient instance."""
        return self._sasl_cli

    @property
    def is_complete(self) -> bool:
        """Return whether the SASL exchange has completed."""
        return bool(self._sasl_cli.complete)

    def next_token(self, challenge: bytes | None = None) -> bytes | None:
        """Process a server challenge and return the next token to send.

        Returns ``None`` if the SASL exchange is complete and no further
        token needs to be transmitted.
        """
        if self.is_complete:
            return None

        try:
            response = self._sasl_cli.process(challenge=challenge)
        except Exception as exc:
            if isinstance(exc, (SASLException, AuthFailedError)):
                raise
            if PURESASL_AVAILABLE:
                if isinstance(exc, puresasl.SASLProtocolException):
                    raise AuthFailedError("protocol error") from exc
                if isinstance(exc, puresasl.SASLError):
                    raise SASLException("library error") from exc
            raise SASLException("SASL step failed") from exc

        if bool(getattr(self._sasl_cli, "complete", False)) and not response:
            return None
        if response is None:
            return b""
        return bytes(response)

    def dispose(self) -> None:
        """Dispose of sensitive credential material in the SASL client."""
        try:
            self._sasl_cli.dispose()
        except Exception:
            pass
