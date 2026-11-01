from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from ghost_routes.config import get_settings
from ghost_routes.persistence.schema import Base

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    # PostGIS/tiger tables are not ours.
    return not (type_ == "table" and reflected and compare_to is None)


def compare_type(ctx, inspected_column, metadata_column, inspected_type, metadata_type):  # type: ignore[no-untyped-def]
    # SRID 0 reflects as unset; compare only what matters for local-frame geometry.
    from geoalchemy2 import Geometry

    if isinstance(metadata_type, Geometry) and isinstance(inspected_type, Geometry):
        return (metadata_type.geometry_type or "GEOMETRY") != (inspected_type.geometry_type or "GEOMETRY")
    return None


def run() -> None:
    url = context.config.attributes.get("url") or get_settings().database_url
    engine = create_engine(url)
    with engine.connect() as conn:
        context.configure(
            connection=conn,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=compare_type,
        )
        with context.begin_transaction():
            context.run_migrations()


run()
