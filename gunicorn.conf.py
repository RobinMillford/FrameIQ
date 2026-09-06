import os

bind = "0.0.0.0:8080"
workers = int(os.environ.get("WEB_WORKERS", 2))
threads = int(os.environ.get("WEB_THREADS", 4))
timeout = 60
graceful_timeout = 30
keepalive = 2
max_requests = 1000
max_requests_jitter = 100
accesslog = "-"
errorlog = "-"
capture_output = True
