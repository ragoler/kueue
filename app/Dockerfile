# Single image for BOTH the controller Deployment and the batch Job pods.
# - Controller: runs `uvicorn controller:app` (the default CMD).
# - Job pods:   the controller stamps `command: ["python", "jobwork.py"]` onto
#               the Jobs it creates, so the same image runs the busy-compute
#               payload. One image -> nothing to keep in version lockstep.
FROM python:3.11-slim

WORKDIR /app

COPY app/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Application code + the playroom UI. Build context is the REPO ROOT (so the
# frontend/ dir, a sibling of app/, can be baked in for standalone UI serving).
COPY app/controller.py app/jobwork.py ./
COPY frontend ./frontend
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# Default entrypoint = the controller. The Job pods override this command.
CMD ["uvicorn", "controller:app", "--host", "0.0.0.0", "--port", "8000"]
