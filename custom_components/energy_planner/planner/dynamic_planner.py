import datetime as dt
import logging

import sqlalchemy
from homeassistant.components.sql import redact_credentials
from homeassistant.components.sql.models import SQLData
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback, Event
from homeassistant.components.recorder.db_schema import (
    States,
    StatesMeta,
)
from homeassistant.components.recorder import (
    get_instance,
    SupportedDialect,
)
from sqlalchemy import select, Result
from sqlalchemy.exc import SQLAlchemyError

from sqlalchemy.orm import Session, scoped_session, sessionmaker

from custom_components.energy_planner import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _validate_and_get_session_maker_for_db_url(db_url: str) -> scoped_session | None:
    """Validate the db_url and return a session maker.

    This does I/O and should be run in the executor.
    """
    sess: Session | None = None
    try:
        engine = sqlalchemy.create_engine(db_url, future=True)
        sessmaker = scoped_session(sessionmaker(bind=engine, future=True))
        # Run a dummy query just to test the db_url
        sess = sessmaker()
        sess.execute(sqlalchemy.text("SELECT 1;"))

    except SQLAlchemyError as err:
        _LOGGER.error(
            "Couldn't connect using %s DB_URL: %s",
            redact_credentials(db_url),
            redact_credentials(str(err)),
        )
        return None
    else:
        return sessmaker
    finally:
        if sess:
            sess.close()


@callback
def _async_get_or_init_domain_data(hass: HomeAssistant) -> SQLData:
    """Get or initialize domain data."""
    if DOMAIN in hass.data and "sql_data" in hass.data[DOMAIN]:
        sql_data: SQLData = hass.data[DOMAIN]["sql_data"]
        return sql_data

    session_makers_by_db_url: dict[str, scoped_session] = {}

    def _shutdown_db_engines(event: Event) -> None:
        """Shutdown all database engines."""
        for sessmaker in session_makers_by_db_url.values():
            sessmaker.connection().engine.dispose()

    cancel_shutdown = hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP, _shutdown_db_engines
    )

    sql_data = SQLData(cancel_shutdown, session_makers_by_db_url)
    hass.data[DOMAIN]["sql_data"] = sql_data
    return sql_data


async def planner(hass: HomeAssistant, *args, **kwargs):
    """Planner."""
    # WIP: Trying to get the historical state of a sensor from the recorder database
    usage_entity = "sensor.sun_next_dawn"
    db_url: str = get_instance(hass).db_url
    try:
        instance = get_instance(hass)
    except KeyError:  # No recorder loaded
        uses_recorder_db = False
    else:
        uses_recorder_db = db_url == instance.db_url
    sessmaker: scoped_session | None
    sql_data = _async_get_or_init_domain_data(hass)
    if uses_recorder_db and instance.dialect_name == SupportedDialect.SQLITE:
        if instance.engine is None:
            raise AssertionError("Recorder is using SQLite but engine is None")
        sessmaker = scoped_session(sessionmaker(bind=instance.engine, future=True))
    elif db_url in sql_data.session_makers_by_db_url:
        sessmaker = sql_data.session_makers_by_db_url[db_url]
    elif sessmaker := await hass.async_add_executor_job(
        _validate_and_get_session_maker_for_db_url, db_url
    ):
        sql_data.session_makers_by_db_url[db_url] = sessmaker
    else:
        return

    def run_query():
        data = None
        sess: scoped_session = sessmaker()
        query = (
            select(States.state)
            .join(StatesMeta, States.metadata_id == StatesMeta.metadata_id)
            .filter(
                StatesMeta.entity_id == usage_entity,
                States.last_updated_ts
                > (dt.datetime.now() - dt.timedelta(days=2)).timestamp(),
            )
            .order_by(States.last_updated_ts.desc())
        )
        try:
            result: Result = sess.execute(query)
        except SQLAlchemyError as err:
            _LOGGER.error(
                "Error executing query %s: %s",
                query,
                redact_credentials(str(err)),
            )
            sess.rollback()
            sess.close()
            return None
        data = result.scalar()
        _LOGGER.info("Result: %s", data)
        sess.close()
        return data

    state_data = await get_instance(hass).async_add_executor_job(run_query)
    _LOGGER.info("state_data: %s", state_data)
