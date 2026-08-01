# ----------------------------------------------------
# 1. BASE IMAGE
# ----------------------------------------------------
FROM python:3.11-slim-bullseye 

# ----------------------------------------------------
# 2. WORKDIR
# ----------------------------------------------------
WORKDIR /app

# ----------------------------------------------------
# 3. SYSTEM DEPENDENCIES (Playwright)
# ----------------------------------------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        wget \
        curl \
        ca-certificates \
        libglib2.0-0 \
        libnspr4 \
        libnss3 \
        libdbus-1-3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libatspi2.0-0 \
        libx11-6 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libxcb1 \
        libxkbcommon0 \
        libasound2 \
        libpangocairo-1.0-0 \
        libgtk-3-0 \
        fonts-liberation \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ----------------------------------------------------
# 4. INSTALL PYTHON DEPENDENCIES
# ----------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ----------------------------------------------------
# 5. 🔥 CRITICAL FIX: INSTALL PLAYWRIGHT BROWSERS
# ----------------------------------------------------
# ✅ Install playwright package FIRST, then install browsers
RUN playwright install chromium

# ----------------------------------------------------
# 6. COPY APP
# ----------------------------------------------------
COPY . /app/

# ----------------------------------------------------
# 7. START COMMAND
# ----------------------------------------------------
# Unbuffers python output so your logs stream instantly to the console
ENV PYTHONUNBUFFERED=1

CMD ["python", "worker/main.py"]