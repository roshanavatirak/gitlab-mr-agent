# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=7860

# Set the working directory
WORKDIR /app

# Install system dependencies (procps is required for start.sh script process checking)
RUN apt-get update && apt-get install -y --no-install-recommends \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . .

# Make start.sh executable and configure line endings (in case written on Windows)
RUN chmod +x start.sh && sed -i 's/\r$//' start.sh

# Expose port 7860 (Hugging Face Spaces default port)
EXPOSE 7860

# Run both Web server and Worker using the superviser script
CMD ["./start.sh"]
