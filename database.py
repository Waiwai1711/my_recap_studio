import os
from datetime import date
from sqlalchemy import create_engine, Column, Integer, String, Date
from sqlalchemy.orm import declarative_base, sessionmaker

# Render.com ပေါ်ရှိ Postgres URL (postgres://) ကို SQLAlchemy အတွက် postgresql:// သို့ ပြင်ဆင်ခြင်း
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

Base.metadata.create_all(bind=engine)
