FROM python:3.11-slim

WORKDIR /app

# Authlib နှင့် Cryptography အတွက် လိုအပ်သော Linux package များ သွင်းယူခြင်း
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render က သတ်မှတ်ပေးမည့် Dynamic PORT ဖြင့် Run ရန် shell command အသုံးပြုခြင်း
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
