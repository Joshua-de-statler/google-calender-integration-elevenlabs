# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the dependencies file to the working directory
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your app's code to the working directory
COPY . .

# Expose the port the app runs on
EXPOSE 8080

# Define the command to run your app
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8080"]