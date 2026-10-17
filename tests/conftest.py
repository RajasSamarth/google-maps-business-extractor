import os

# Ensure tests run with synthetic mock source and no rate limits
os.environ["USE_PLAYWRIGHT_SOURCE"] = "0"
os.environ["RATE_LIMIT_PER_MINUTE"] = "10000"
os.environ["MAX_JOBS_PER_HOUR"] = "0"
