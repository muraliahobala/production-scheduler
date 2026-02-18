# Use slim Python image (smaller, faster)
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements first (for Docker layer caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy your code
COPY . .

# Expose port 10000 (Render requirement)
EXPOSE 10000

# Run with production settings (4 workers)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000", "--workers", "4"]
