# Dockerfile

# ---- Build Stage ----
# Use a full Python image to build dependencies, which may have system-level requirements.
FROM python:3.9 as builder

# Set the working directory
WORKDIR /app

# Install build-time dependencies
RUN pip install --upgrade pip

# Copy only the requirements file and install dependencies
# This leverages Docker's layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# ---- Final Stage ----
# Use a slim image for the final container to reduce its size.
FROM python:3.9-slim

# Set the working directory
WORKDIR /app

# Create a non-root user for security
RUN useradd --create-home appuser
USER appuser

# Copy the installed dependencies from the builder stage
COPY --from=builder /usr/local/lib/python3.9/site-packages /usr/local/lib/python3.9/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy the application code
COPY . .

# Expose the port the app runs on
EXPOSE 8080

# Define the command to run your app
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080"]