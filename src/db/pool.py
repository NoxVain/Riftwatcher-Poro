import queue
import threading

try:
    import psycopg
except ImportError:
    psycopg = None

from src.config import DATABASE_URL, DB_POOL_SIZE


if psycopg is None:
    raise RuntimeError("DATABASE_URL is set but psycopg is not installed. Add psycopg[binary] to dependencies.")

DB_ENABLED = True
DB_POOL = None
DB_POOL_LOCK = threading.Lock()
DB_POOL_TOTAL = 0


def init_db_pool():
    global DB_POOL
    if DB_POOL is None:
        DB_POOL = queue.LifoQueue(maxsize=max(1, DB_POOL_SIZE))


def create_db_connection():
    return psycopg.connect(DATABASE_URL)


def db_acquire_connection():
    global DB_POOL_TOTAL
    if DB_POOL is None:
        init_db_pool()
    try:
        return DB_POOL.get_nowait()
    except queue.Empty:
        pass

    should_create = False
    with DB_POOL_LOCK:
        if DB_POOL_TOTAL < max(1, DB_POOL_SIZE):
            DB_POOL_TOTAL += 1
            should_create = True

    if should_create:
        try:
            return create_db_connection()
        except Exception:
            with DB_POOL_LOCK:
                DB_POOL_TOTAL = max(0, DB_POOL_TOTAL - 1)
            raise

    return DB_POOL.get()


def db_release_connection(conn, discard=False):
    global DB_POOL_TOTAL
    if conn is None:
        return
    if discard:
        try:
            conn.close()
        finally:
            with DB_POOL_LOCK:
                DB_POOL_TOTAL = max(0, DB_POOL_TOTAL - 1)
        return
    try:
        DB_POOL.put_nowait(conn)
    except queue.Full:
        try:
            conn.close()
        finally:
            with DB_POOL_LOCK:
                DB_POOL_TOTAL = max(0, DB_POOL_TOTAL - 1)


def db_execute(query, params=None, fetch=False, fetchone=False):
    conn = db_acquire_connection()
    discard_conn = False
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            if fetchone:
                result = cur.fetchone()
                conn.commit()
                return result
            if fetch:
                result = cur.fetchall()
                conn.commit()
                return result
        conn.commit()
    except Exception:
        discard_conn = True
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        db_release_connection(conn, discard=discard_conn)
    return None
