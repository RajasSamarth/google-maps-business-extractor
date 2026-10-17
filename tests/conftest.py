import os

# Ensure tests run with synthetic mock source, valid API_KEY, and no rate limits
os.environ["API_KEY"] = "change-me-in-development"
os.environ["USE_PLAYWRIGHT_SOURCE"] = "0"
os.environ["RATE_LIMIT_PER_MINUTE"] = "10000"
os.environ["MAX_JOBS_PER_HOUR"] = "0"
