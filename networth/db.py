"""Engine creation, schema init, and small settings-table helpers."""
import json

import sqlalchemy as sa

import config
from models import metadata, settings


def get_engine(db_url: str | None = None):
    return sa.create_engine(db_url or config.DB_URL, future=True)


def init_db(engine=None):
    engine = engine or get_engine()
    metadata.create_all(engine)
    return engine


def get_setting(conn, key: str, default=None):
    row = conn.execute(
        sa.select(settings.c.value_json).where(settings.c.key == key)
    ).fetchone()
    if row is None:
        return default
    return json.loads(row[0])


def set_setting(conn, key: str, value):
    payload = json.dumps(value)
    existing = conn.execute(
        sa.select(settings.c.key).where(settings.c.key == key)
    ).fetchone()
    if existing:
        conn.execute(
            settings.update().where(settings.c.key == key).values(value_json=payload)
        )
    else:
        conn.execute(settings.insert().values(key=key, value_json=payload))


def get_rule_config(conn) -> dict:
    """RULE_DEFAULTS overlaid with any rows stored in the settings table."""
    cfg = dict(config.RULE_DEFAULTS)
    for key in cfg:
        override = get_setting(conn, key, None)
        if override is not None:
            cfg[key] = override
    return cfg
