from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Float, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

DATABASE_URL = "sqlite:///./recap_studio.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    name = Column(String)
    avatar = Column(String)
    daily_credits_left = Column(Integer, default=2)
    package_credits = Column(Integer, default=0)
    last_reset_date = Column(DateTime, default=datetime.utcnow)
    is_admin = Column(Boolean, default=False)
    is_banned = Column(Boolean, default=False)
    referral_code = Column(String, unique=True, index=True)
    referred_by = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class ProjectHistory(Base):
    __tablename__ = "project_history"
    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, index=True)
    title = Column(String, default="Untitled Recap")
    duration_sec = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)

class LogoPreset(Base):
    __tablename__ = "logo_presets"
    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, index=True)
    name = Column(String)
    file_path = Column(String)

class PaymentRequest(Base):
    __tablename__ = "payment_requests"
    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, index=True)
    package_type = Column(String)
    amount = Column(Integer)
    payment_method = Column(String)
    slip_url = Column(String)
    status = Column(String, default="pending")
    telegram_msg_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Package(Base):
    __tablename__ = "packages"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String)
    credits = Column(Integer)
    price_mmk = Column(Integer)
    is_active = Column(Boolean, default=True)

Base.metadata.create_all(bind=engine)
