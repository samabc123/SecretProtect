FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum *.go ./
COPY static ./static
COPY migrations ./migrations
COPY deploy ./deploy
RUN go test ./...
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/archive-proxy .

FROM scratch
COPY --from=build /out/archive-proxy /archive-proxy
USER 65532:65532
ENTRYPOINT ["/archive-proxy"]
