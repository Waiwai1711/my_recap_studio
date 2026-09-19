FROM python:3.11-slim

# Linux ထဲတွင် FFmpeg ကို အလိုအလျောက် သွင်းယူခြင်း
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# လိုအပ်သော Python Library များ သွင်းခြင်း
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ကုဒ်နှင့် ဖိုင်များ အားလုံးကို Container ထဲ ကူးထည့်ခြင်း
COPY . .

# Server Port ဖွင့်ပေးခြင်း
EXPOSE 8000

# Web App စတင် run ခြင်း
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
