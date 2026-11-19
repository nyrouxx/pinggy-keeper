FROM python:3.13-slim-bookworm

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY keeper.py ./
USER 65532:65532
CMD ["python", "-Bu", "keeper.py"]
