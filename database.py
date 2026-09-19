import os
from datetime import date, datetime
from sqlalchemy import create_engine, Column, Integer, String, Date, DateTime, Boolean, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./recap_studio.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, default="User")
    avatar = Column(String, default="")
    daily_credits_left = Column(Integer, default=2)
    package_credits = Column(Integer, default=0)
    last_reset_date = Column(Date, default=date.today)
    is_admin = Column(Boolean, default=False)
    is_banned = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class PaymentRequest(Base):
    __tablename__ = "payment_requests"
    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, index=True)
    package_type = Column(String)  # ပုဒ်ရေ (e.g. "10")
    amount = Column(Integer)        # ကျပ်ငွေ (e.g. 5000)
    payment_method = Column(String)
    slip_url = Column(String)
    status = Column(String, default="pending")  # pending, approved, rejected
    telegram_msg_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Package(Base):
    __tablename__ = "packages"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)         # ဥပမာ - "၁၀ ပုဒ်"
    credits = Column(Integer)     # ပုဒ်ရေ (e.g. 10)
    price_mmk = Column(Integer)   # ဈေးနှုန်း (e.g. 5000)
    is_active = Column(Boolean, default=True)

Base.metadata.create_all(bind=engine)

# Auto-migration for existing SQLite Database & Default Packages Seeding
db = SessionLocal()
try:
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_banned BOOLEAN DEFAULT 0"))
            conn.commit()
        except Exception:
            pass

    if db.query(Package).count() == 0:
        db.add_all([
            Package(name="၁၀ ပုဒ်", credits=10, price_mmk=5000, is_active=True),
            Package(name="၂၀ ပုဒ်", credits=20, price_mmk=10000, is_active=True),
            Package(name="၃၀ ပုဒ်", credits=30, price_mmk=15000, is_active=True)
        ])
        db.commit()
except Exception as e:
    print("Database init notice:", e)
finally:
    db.close()
