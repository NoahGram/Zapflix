# Use official Python runtime as a parent image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create directory for downloads if not exists (optional)
RUN mkdir -p /downloads

# Expose port (FastAPI defaults to 8000)
EXPOSE 8000

# Run the server
CMD ["uvicorn", "web_server:app", "--host", "0.0.0.0", "--port", "8000"]
