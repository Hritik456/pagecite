# Build context: repo root. Flyway with this commit's migrations baked in (pagecite-migrate:<sha>), so the
# server runs exactly the migrations of the code it deploys, from an image, with no repo checkout.
FROM flyway/flyway:11
COPY db/migrations /flyway/migrations
ARG GIT_SHA=dev
LABEL org.opencontainers.image.revision=$GIT_SHA
