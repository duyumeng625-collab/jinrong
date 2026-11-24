"""Durable SQL storage for conversation history, tickets, and customer orders.

SQLite is the zero-configuration default. Set APP_DATABASE_URL to a
SQLAlchemy URL such as mysql+pymysql://user:password@localhost:3306/smartcs
to use MySQL.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    create_engine,
    event,
    inspect as sqlalchemy_inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.engine import Engine, URL, make_url


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).isoformat()
    return str(value)


_ORDER_ID_PATTERN = re.compile(r"\bORD-[A-Za-z0-9][A-Za-z0-9-]*\b", re.IGNORECASE)


class SQLStore:
    """SQL source of truth for complete chat logs and ticket records."""

    def __init__(self, database_url: str | None = None):
        self.database_url = (
            database_url
            or os.getenv("APP_DATABASE_URL")
            or self._mysql_url_from_components()
            or "sqlite:///./data/app.sqlite3"
        )
        url = make_url(self.database_url)
        connect_args: dict[str, Any] = {}
        if url.get_backend_name() == "sqlite":
            connect_args = {"check_same_thread": False, "timeout": 30}
            if url.database and url.database != ":memory:":
                Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

        self.engine: Engine = create_engine(
            self.database_url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        if url.get_backend_name() == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        self.metadata = MetaData()
        self.conversations = Table(
            "conversations",
            self.metadata,
            Column("session_id", String(128), primary_key=True),
            Column("user_id", String(128), nullable=False),
            Column("created_at", DateTime, nullable=False),
            Column("updated_at", DateTime, nullable=False),
        )
        self.messages = Table(
            "conversation_messages",
            self.metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column(
                "session_id",
                String(128),
                ForeignKey("conversations.session_id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            Column("role", String(16), nullable=False),
            Column("content", Text, nullable=False),
            Column("created_at", DateTime, nullable=False),
        )
        self.tickets = Table(
            "tickets",
            self.metadata,
            Column("ticket_id", String(64), primary_key=True),
            Column("type", String(80), nullable=False),
            Column("priority", String(24), nullable=False),
            Column("status", String(32), nullable=False),
            Column("summary", Text, nullable=False),
            Column("details", Text, nullable=False),
            Column(
                "order_id",
                String(64),
                ForeignKey("orders.order_id", name="fk_tickets_order_id", ondelete="RESTRICT"),
                nullable=True,
                index=True,
            ),
            Column("user_id", String(128), nullable=False, index=True),
            Column("created_at", DateTime, nullable=False),
            Column("updated_at", DateTime, nullable=False),
        )
        self.orders = Table(
            "orders",
            self.metadata,
            Column("order_id", String(64), primary_key=True),
            Column("user_id", String(128), nullable=False, index=True),
            Column("product", String(255), nullable=False),
            Column("amount", Numeric(14, 2), nullable=False),
            Column("status", String(32), nullable=False, index=True),
            Column("created_at", DateTime, nullable=False),
            Column("updated_at", DateTime, nullable=False),
            Column("shipping_company", String(128), nullable=True),
            Column("tracking_number", String(128), nullable=True),
            Column("remark", Text, nullable=True),
        )
        self.products = Table(
            "products",
            self.metadata,
            Column("product_code", String(64), primary_key=True),
            Column("name", String(255), nullable=False, index=True),
            Column("category", String(80), nullable=False, index=True),
            Column("issuer", String(255), nullable=True),
            Column("currency", String(12), nullable=False, default="CNY", server_default=text("'CNY'")),
            Column("risk_level", String(32), nullable=True),
            Column("min_purchase", Numeric(14, 2), nullable=True),
            Column("term_days", Integer, nullable=True),
            Column("rate_info", Text, nullable=True),
            Column("description", Text, nullable=False),
            Column("official_source", String(512), nullable=True),
            Column("status", String(24), nullable=False, default="active", server_default=text("'active'"), index=True),
            Column(
                "updated_at",
                DateTime,
                nullable=False,
                default=_utc_now,
                onupdate=_utc_now,
                server_default=text("CURRENT_TIMESTAMP"),
            ),
        )
        self.metadata.create_all(self.engine)
        self._migrate_ticket_order_id()
        self._backfill_ticket_order_links()

    def _migrate_ticket_order_id(self) -> None:
        """Add a nullable order relationship to existing ticket tables."""
        inspector = sqlalchemy_inspect(self.engine)
        columns = {column["name"] for column in inspector.get_columns("tickets")}
        dialect = self.engine.dialect.name

        if "order_id" not in columns:
            if dialect == "sqlite":
                statement = (
                    "ALTER TABLE tickets ADD COLUMN order_id VARCHAR(64) "
                    "REFERENCES orders(order_id) ON DELETE RESTRICT"
                )
            else:
                statement = "ALTER TABLE tickets ADD COLUMN order_id VARCHAR(64) NULL"
            with self.engine.begin() as connection:
                connection.execute(text(statement))

        inspector = sqlalchemy_inspect(self.engine)
        indexes = inspector.get_indexes("tickets")
        if not any(index.get("column_names") == ["order_id"] for index in indexes):
            Index("ix_tickets_order_id", self.tickets.c.order_id).create(
                self.engine, checkfirst=True
            )

        if dialect in {"mysql", "mariadb", "postgresql"}:
            foreign_keys = inspector.get_foreign_keys("tickets")
            has_order_fk = any(
                fk.get("constrained_columns") == ["order_id"]
                and fk.get("referred_table") == "orders"
                and fk.get("referred_columns") == ["order_id"]
                for fk in foreign_keys
            )
            if not has_order_fk:
                with self.engine.begin() as connection:
                    connection.execute(text(
                        "ALTER TABLE tickets ADD CONSTRAINT fk_tickets_order_id "
                        "FOREIGN KEY (order_id) REFERENCES orders(order_id) ON DELETE RESTRICT"
                    ))

    def _backfill_ticket_order_links(self) -> None:
        """Link legacy tickets only when one exact order ID belongs to the same user."""
        with self.engine.begin() as connection:
            rows = connection.execute(
                select(
                    self.tickets.c.ticket_id,
                    self.tickets.c.user_id,
                    self.tickets.c.summary,
                    self.tickets.c.details,
                ).where(self.tickets.c.order_id.is_(None))
            ).mappings().all()

            for ticket in rows:
                candidates = set(_ORDER_ID_PATTERN.findall(
                    f"{ticket['summary']}\n{ticket['details']}"
                ))
                if len(candidates) != 1:
                    continue
                order_id = next(iter(candidates)).upper()
                order = connection.execute(
                    select(self.orders.c.order_id).where(
                        self.orders.c.order_id == order_id,
                        self.orders.c.user_id == ticket["user_id"],
                    )
                ).first()
                if order:
                    connection.execute(
                        update(self.tickets)
                        .where(self.tickets.c.ticket_id == ticket["ticket_id"])
                        .values(order_id=order_id)
                    )

    @staticmethod
    def _mysql_url_from_components() -> str | None:
        host = os.getenv("SQL_DB_HOST", "").strip()
        database = os.getenv("SQL_DB_NAME", "").strip()
        if not host or not database:
            return None
        username = os.getenv("SQL_DB_USER", "").strip()
        if not username:
            raise ValueError("SQL_DB_USER is required when MySQL host and database are configured")
        url = URL.create(
            "mysql+pymysql",
            username=username,
            password=os.getenv("SQL_DB_PASSWORD", ""),
            host=host,
            port=int(os.getenv("SQL_DB_PORT", "3306")),
            database=database,
            query={"charset": os.getenv("SQL_DB_CHARSET", "utf8mb4")},
        )
        return url.render_as_string(hide_password=False)

    def ping(self) -> bool:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True

    def _touch_conversation(self, connection, session_id: str, user_id: str, now: datetime) -> None:
        values = {
            "session_id": session_id,
            "user_id": user_id,
            "created_at": now,
            "updated_at": now,
        }
        dialect = self.engine.dialect.name
        if dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert

            statement = dialect_insert(self.conversations).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=[self.conversations.c.session_id],
                set_={"user_id": user_id, "updated_at": now},
            )
            connection.execute(statement)
        elif dialect == "mysql":
            from sqlalchemy.dialects.mysql import insert as dialect_insert

            statement = dialect_insert(self.conversations).values(**values)
            statement = statement.on_duplicate_key_update(user_id=user_id, updated_at=now)
            connection.execute(statement)
        else:
            existing = connection.execute(
                select(self.conversations.c.session_id).where(
                    self.conversations.c.session_id == session_id
                )
            ).first()
            if existing:
                connection.execute(
                    update(self.conversations)
                    .where(self.conversations.c.session_id == session_id)
                    .values(user_id=user_id, updated_at=now)
                )
            else:
                connection.execute(self.conversations.insert().values(**values))

    def append_message(self, session_id: str, user_id: str, role: str, content: str) -> dict[str, str]:
        now = _utc_now()
        with self.engine.begin() as connection:
            self._touch_conversation(connection, session_id, user_id, now)
            connection.execute(
                self.messages.insert().values(
                    session_id=session_id,
                    role=role,
                    content=content,
                    created_at=now,
                )
            )
        return {"role": role, "content": content, "timestamp": _timestamp(now)}

    def get_messages(self, session_id: str, limit: int | None = None) -> list[dict[str, str]]:
        statement = (
            select(
                self.messages.c.role,
                self.messages.c.content,
                self.messages.c.created_at,
            )
            .where(self.messages.c.session_id == session_id)
            .order_by(self.messages.c.id.desc() if limit else self.messages.c.id.asc())
        )
        if limit is not None:
            statement = statement.limit(max(0, limit))
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(statement).mappings()]
        if limit is not None:
            rows.reverse()
        return [
            {
                "role": row["role"],
                "content": row["content"],
                "timestamp": _timestamp(row["created_at"]),
            }
            for row in rows
        ]

    def create_ticket(
        self,
        ticket_id: str,
        ticket_type: str,
        priority: str,
        status: str,
        summary: str,
        details: str,
        user_id: str,
        order_id: str | None = None,
    ) -> dict[str, Any]:
        order_id = order_id.strip().upper() if order_id else None
        if ticket_type == "refund" and not order_id:
            raise ValueError("退款工单必须关联订单，请先提供订单号")

        now = _utc_now()
        ticket = {
            "ticket_id": ticket_id,
            "type": ticket_type,
            "priority": priority,
            "status": status,
            "summary": summary,
            "details": details,
            "order_id": order_id,
            "user_id": user_id,
            "created_at": now,
            "updated_at": now,
        }
        with self.engine.begin() as connection:
            if order_id:
                matching_order = connection.execute(
                    select(self.orders.c.order_id).where(
                        self.orders.c.order_id == order_id,
                        self.orders.c.user_id == user_id,
                    )
                ).first()
                if not matching_order:
                    raise ValueError("订单不存在或不属于当前用户，无法关联退款工单")
            connection.execute(self.tickets.insert().values(**ticket))
        return self._ticket_dict(ticket)

    @staticmethod
    def _ticket_dict(ticket: dict[str, Any]) -> dict[str, Any]:
        return {
            **ticket,
            "created_at": _timestamp(ticket["created_at"]),
            "updated_at": _timestamp(ticket["updated_at"]),
        }

    def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(self.tickets).where(self.tickets.c.ticket_id == ticket_id)
            ).mappings().first()
        return self._ticket_dict(dict(row)) if row else None

    def get_tickets_by_user(self, user_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(self.tickets)
                .where(self.tickets.c.user_id == user_id)
                .order_by(self.tickets.c.created_at.desc())
            ).mappings().all()
        return [self._ticket_dict(dict(row)) for row in rows]

    def get_tickets_by_order(self, order_id: str, user_id: str) -> list[dict[str, Any]]:
        """Return tickets linked to an order owned by the specified user."""
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(self.tickets)
                .where(
                    self.tickets.c.order_id == order_id,
                    self.tickets.c.user_id == user_id,
                )
                .order_by(self.tickets.c.created_at.desc())
            ).mappings().all()
        return [self._ticket_dict(dict(row)) for row in rows]

    def update_ticket_status(self, ticket_id: str, status: str) -> dict[str, Any] | None:
        with self.engine.begin() as connection:
            connection.execute(
                update(self.tickets)
                .where(self.tickets.c.ticket_id == ticket_id)
                .values(status=status, updated_at=_utc_now())
            )
            row = connection.execute(
                select(self.tickets).where(self.tickets.c.ticket_id == ticket_id)
            ).mappings().first()
        return self._ticket_dict(dict(row)) if row else None

    def get_orders(
        self,
        order_id: str | None = None,
        user_id: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Query orders, optionally scoped to an order ID and/or customer."""
        statement = select(self.orders).order_by(self.orders.c.created_at.desc())
        if order_id:
            statement = statement.where(self.orders.c.order_id == order_id)
        if user_id:
            statement = statement.where(self.orders.c.user_id == user_id)
        if not order_id and not user_id:
            raise ValueError("查询订单时必须提供订单号或用户身份")
        statement = statement.limit(max(1, min(int(limit), 50)))
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(statement).mappings()]
        return [
            {
                **row,
                "amount": f"{row['amount']:.2f}",
                "created_at": _timestamp(row["created_at"]),
                "updated_at": _timestamp(row["updated_at"]),
            }
            for row in rows
        ]

    def get_products(
        self,
        keyword: str | None = None,
        product_code: str | None = None,
        category: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return active catalog records matching a code, keyword, or category."""
        statement = select(self.products).where(self.products.c.status == "active")
        if product_code and product_code.strip():
            statement = statement.where(
                self.products.c.product_code == product_code.strip().upper()
            )
        if keyword and keyword.strip():
            pattern = f"%{keyword.strip()}%"
            statement = statement.where(or_(
                self.products.c.product_code.ilike(pattern),
                self.products.c.name.ilike(pattern),
                self.products.c.category.ilike(pattern),
                self.products.c.issuer.ilike(pattern),
                self.products.c.rate_info.ilike(pattern),
                self.products.c.description.ilike(pattern),
            ))
        if category and category.strip():
            statement = statement.where(self.products.c.category.ilike(f"%{category.strip()}%"))
        statement = statement.order_by(self.products.c.name.asc()).limit(
            max(1, min(int(limit), 50))
        )
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(statement).mappings()]
        return [
            {
                **row,
                "min_purchase": (
                    f"{row['min_purchase']:.2f}" if row["min_purchase"] is not None else None
                ),
                "updated_at": _timestamp(row["updated_at"]),
            }
            for row in rows
        ]
