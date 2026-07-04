"""
database.py

SQLite persistence layer for the Shopify product monitor.
Tracks products at the product level (title, handle, description) and
variants at the variant level (price, availability). Giveaway items
are frequently a single variant within a multi-variant product, so
price/availability must never be tracked at the product level alone.
"""

import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_DIR = BASE_DIR / "data"
DB_PATH = DB_DIR / "shopify_monitor.db"


def get_connection():
    DB_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables():
    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                product_id INTEGER PRIMARY KEY,
                title TEXT,
                handle TEXT,
                description_text TEXT,
                description_hash TEXT,
                last_seen TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS variants (
                variant_id INTEGER PRIMARY KEY,
                product_id INTEGER NOT NULL,
                title TEXT,
                price TEXT,
                available INTEGER,
                last_seen TEXT,
                FOREIGN KEY (product_id) REFERENCES products(product_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def product_exists(product_id):
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT 1 FROM products WHERE product_id = ? LIMIT 1",
            (product_id,),
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


def insert_product(product):
    """
    Expected keys: product_id, title, handle, description_text,
    description_hash, last_seen
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO products (
                product_id, title, handle, description_text,
                description_hash, last_seen
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                product["product_id"],
                product["title"],
                product["handle"],
                product["description_text"],
                product["description_hash"],
                product["last_seen"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_product(product):
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE products
            SET title = ?,
                handle = ?,
                description_text = ?,
                description_hash = ?,
                last_seen = ?
            WHERE product_id = ?
            """,
            (
                product["title"],
                product["handle"],
                product["description_text"],
                product["description_hash"],
                product["last_seen"],
                product["product_id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_product(product_id):
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM products WHERE product_id = ?",
            (product_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_variants(product_id):
    """
    Returns {variant_id: variant_row_dict} for all stored variants
    belonging to a product. Empty dict if the product has no stored
    variants yet.
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM variants WHERE product_id = ?",
            (product_id,),
        )
        return {row["variant_id"]: dict(row) for row in cursor.fetchall()}
    finally:
        conn.close()


def variant_exists(variant_id):
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT 1 FROM variants WHERE variant_id = ? LIMIT 1",
            (variant_id,),
        )
        return cursor.fetchone() is not None
    finally:
        conn.close()


def insert_variant(variant):
    """
    Expected keys: variant_id, product_id, title, price, available, last_seen
    """
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO variants (
                variant_id, product_id, title, price, available, last_seen
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                variant["variant_id"],
                variant["product_id"],
                variant["title"],
                variant["price"],
                int(variant["available"]),
                variant["last_seen"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_variant(variant):
    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE variants
            SET title = ?,
                price = ?,
                available = ?,
                last_seen = ?
            WHERE variant_id = ?
            """,
            (
                variant["title"],
                variant["price"],
                int(variant["available"]),
                variant["last_seen"],
                variant["variant_id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_metadata(key):
    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT value FROM app_metadata WHERE key = ?",
            (key,),
        )
        row = cursor.fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_metadata(key, value):
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO app_metadata (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()