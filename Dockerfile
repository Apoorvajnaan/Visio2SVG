FROM ifx-base-python-sample:3.12

# Set the working directory to /app
WORKDIR /app

# Copy the requirements file
COPY requirements.txt .

# Install the dependencies
RUN pip install -r requirements.txt

# Copy the application code
COPY . .

# Expose the port
EXPOSE 8000

# Run the command to start the application
CMD ["python", "app.py"]
