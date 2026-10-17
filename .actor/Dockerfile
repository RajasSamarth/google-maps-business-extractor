# Use Apify's official Python 3.12 image with Playwright & Chromium pre-installed
FROM apify/actor-python-playwright:3.12

# Copy requirements first to leverage Docker layer caching
COPY requirements.txt ./

# Install project dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . ./

# Run the Apify Actor entrypoint
CMD ["python3", "-m", "app.actor"]
