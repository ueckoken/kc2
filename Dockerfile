FROM python:3.9

RUN pip install poetry

WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN poetry install
COPY . .

EXPOSE 8000
CMD poetry run uvicorn main:app --host 0.0.0.0
