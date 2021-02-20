FROM golang:1.15 as install-transocks

ENV GO111MODULE on

RUN git clone https://github.com/cybozu-go/transocks -b v1.1.1 $GOPATH/src/github.com/cybouzu-go/transocks \
 && cd $GOPATH/src/github.com/cybouzu-go/transocks \
 && go install ./...

FROM python:3.9

RUN pip install poetry

WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN poetry install
COPY . .

COPY --from=install-transocks /go/bin/transocks ./bin/transocks

EXPOSE 8000
CMD poetry run uvicorn main:app --host 0.0.0.0
