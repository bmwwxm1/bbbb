import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bot.config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_size=5, max_overflow=10)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Collection(Base):
    __tablename__ = "collections"

    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    floor_price_nanoton: Mapped[int] = mapped_column(BigInteger, default=0)
    prev_day_floor_nanoton: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    is_new: Mapped[bool] = mapped_column(Boolean, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    craftable: Mapped[bool] = mapped_column(Boolean, default=False)
    getgems_floor_nanoton: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PriceMatrix(Base):
    __tablename__ = "price_matrix"
    __table_args__ = (
        Index(
            "ix_price_matrix_combo",
            "collection_name",
            "model_name",
            "backdrop_name",
            "symbol_name",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_name: Mapped[str] = mapped_column(String(256))
    model_name: Mapped[str] = mapped_column(String(256), default="")
    backdrop_name: Mapped[str] = mapped_column(String(256), default="")
    symbol_name: Mapped[str] = mapped_column(String(256), default="")
    floor_price: Mapped[int] = mapped_column(BigInteger, default=0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    median_price: Mapped[float] = mapped_column(Float, default=0.0)
    top_buy_order: Mapped[int] = mapped_column(BigInteger, default=0)
    listings_count: Mapped[int] = mapped_column(Integer, default=0)
    sales_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gift_id: Mapped[str] = mapped_column(String(256))
    collection_name: Mapped[str] = mapped_column(String(256))
    model_name: Mapped[str] = mapped_column(String(256), default="")
    backdrop_name: Mapped[str] = mapped_column(String(256), default="")
    symbol_name: Mapped[str] = mapped_column(String(256), default="")
    buy_price: Mapped[int] = mapped_column(BigInteger)
    sell_price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sell_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    roi_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_type: Mapped[str] = mapped_column(String(32), default="")
    target_sell_price: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    nft_address: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="bought")
    # Statuses: bought → withdrawn → awaiting_transfer → listed_getgems → sold
    #           bought → listed → sold  (MRKT-only)
    bought_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sold_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    listed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (
        Index(
            "ix_price_history_combo",
            "collection_name",
            "model_name",
            "backdrop_name",
            "symbol_name",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    collection_name: Mapped[str] = mapped_column(String(256))
    model_name: Mapped[str] = mapped_column(String(256), default="")
    backdrop_name: Mapped[str] = mapped_column(String(256), default="")
    symbol_name: Mapped[str] = mapped_column(String(256), default="")
    price: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BotSettings(Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
