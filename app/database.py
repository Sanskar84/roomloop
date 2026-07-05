from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

DATABASE_URL = "sqlite:///./roomloop.db"

# timeout=30 lets a second writer wait for the lock instead of failing immediately
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)


# SQLite's default DEFERRED transactions allow concurrent readers, so two
# requests could both pass the conflict-check SELECT before either INSERTs
# (check-then-insert race -> double booking). BEGIN IMMEDIATE acquires the
# write lock at transaction start, serializing writers so the conflict check
# and insert run atomically. Standard SQLAlchemy/pysqlite recipe:
@event.listens_for(engine, "connect")
def _sqlite_take_over_transactions(dbapi_connection, connection_record):
    dbapi_connection.isolation_level = None


@event.listens_for(engine, "begin")
def _sqlite_begin_immediate(conn):
    conn.exec_driver_sql("BEGIN IMMEDIATE")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
