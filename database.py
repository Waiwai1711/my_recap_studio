import os
from datetime import date, datetime
from sqlalchemy import create_engine, Column, Integer, String, Date, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./recap_studio.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    name = Column(String)
    avatar = Column(String)
    
    # Premium ပုဒ်ရေနှင့် နေ့စဉ် အခမဲ့ ၂ ပုဒ်
    package_credits = Column(Integer, default=0)
    daily_credits_left = Column(Integer, default=2)
    last_reset_date = Column(Date, default=date.today)
    
    # Admin စစ်ဆေးရန် (True ဖြစ်ပါက Admin Panel ဝင်ခွင့်ရမည်)
    is_admin = Column(Boolean, default=False)

class PaymentRequest(Base):
    __tablename__ = "payment_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_email = Column(String, nullable=False)
    package_type = Column(String, nullable=False) # "10", "20", "30"
    amount = Column(Integer, nullable=False)       # 5000, 10000, 15000
    payment_method = Column(String, nullable=False) # KPay, WavePay, AYAPay
    slip_url = Column(String, nullable=False)     # ပြေစာ ပုံလမ်းကြောင်း
    status = Column(String, default="pending")    # pending, approved, rejected
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)
