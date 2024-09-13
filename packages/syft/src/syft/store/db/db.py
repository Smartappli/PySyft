# stdlib
import logging
from typing import Generic
from typing import TypeVar
from urllib.parse import urlparse

# third party
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# relative
from ...serde.serializable import serializable
from ...server.credentials import SyftVerifyKey
from ...types.uid import UID
from .schema import PostgresBase
from .schema import SQLiteBase

logger = logging.getLogger(__name__)


@serializable(canonical_name="DBConfig", version=1)
class DBConfig(BaseModel):
    reset: bool = False

    @property
    def connection_string(self) -> str:
        raise NotImplementedError("Subclasses must implement this method.")

    @classmethod
    def from_connection_string(cls, conn_str: str) -> "DBConfig":
        # relative
        from .postgres import PostgresDBConfig
        from .sqlite import SQLiteDBConfig

        parsed = urlparse(conn_str)
        if parsed.scheme == "postgresql":
            return PostgresDBConfig(
                host=parsed.hostname,
                port=parsed.port,
                user=parsed.username,
                password=parsed.password,
                database=parsed.path.lstrip("/"),
            )
        elif parsed.scheme == "sqlite":
            return SQLiteDBConfig(path=parsed.path)
        else:
            raise ValueError(f"Unsupported database scheme {parsed.scheme}")


ConfigT = TypeVar("ConfigT", bound=DBConfig)


class DBManager(Generic[ConfigT]):
    def __init__(
        self,
        config: ConfigT,
        server_uid: UID,
        root_verify_key: SyftVerifyKey,
    ) -> None:
        self.config = config
        self.root_verify_key = root_verify_key
        self.server_uid = server_uid
        self.engine = create_engine(
            config.connection_string,
            # json_serializer=dumps,
            # json_deserializer=loads,
        )
        logger.info(f"Connecting to {config.connection_string}")
        self.sessionmaker = sessionmaker(bind=self.engine)
        self.update_settings()
        logger.info(f"Successfully connected to {config.connection_string}")

    def update_settings(self) -> None:
        pass

    def init_tables(self) -> None:
        Base = SQLiteBase if self.engine.dialect.name == "sqlite" else PostgresBase
        if self.config.reset:
            # drop all tables that we know about
            Base.metadata.drop_all(bind=self.engine)
            self.config.reset = False
        Base.metadata.create_all(self.engine)

    def reset(self) -> None:
        Base = SQLiteBase if self.engine.dialect.name == "sqlite" else PostgresBase
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(self.engine)
